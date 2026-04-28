"""FastAPI 入口。"""

from __future__ import annotations

import json
import logging
import secrets
import sys
import time
from collections.abc import AsyncGenerator
from dataclasses import asdict, replace
from pathlib import Path
from typing import Annotated, Literal

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .config import Settings, SettingsStore, load_settings, mask_secret
from .db import LogDB
from .curl_import import CurlImportError, parse_grok_curl
from .grok_client import (
    GrokClientError,
    IMAGE_DIR,
    ImageResult,
    SKILLS_URL,
    _headers,
    _init_session,
    _session_kwargs,
    flaresolverr_destroy_session,
    flaresolverr_refresh_cf,
    merge_grok_cookies,
    parse_cookie_string,
    complete_chat,
    create_video,
    delete_grok_asset,
    get_video_link,
    list_grok_assets,
    resolve_aspect_ratio,
    save_grok_asset_local,
    smoke_skills,
    stream_chat,
    stream_grok_asset,
)
from .image_stream import ImageStreamWorker, StreamConfig
from .models import get_model, list_models, model_to_openai
from .monitor import Monitor
from .openai_compat import chat_response, error_payload, response_id, sse_data, sse_error, stream_chunk
from .schemas import ChatCompletionRequest, ImageGenerationRequest, ImageStreamStartRequest, TaskQueueAddRequest, VideoGenerationRequest
from .task_queue import TaskQueue
from .ws_gateway import WsGateway

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

_TAG_OPENAI = "OpenAI 兼容"
_TAG_STREAM = "连续生图"
_TAG_TASKS  = "任务队列"
_TAG_GALLERY = "图库 & 会话"
_TAG_VIDEO  = "视频生成"
_TAG_QUOTA  = "额度查询"
_TAG_LOGS   = "请求日志"
_TAG_ADMIN  = "管理 & 配置"

_TAGS_META = [
    {"name": _TAG_OPENAI,  "description": "兼容 OpenAI SDK 的接口，可直接用现有客户端调用。认证：`Authorization: Bearer <api_key>`。"},
    {"name": _TAG_STREAM,  "description": "后台持续生图 worker：启动 / 停止 / 状态轮询。同一时刻只允许一个 worker 运行。"},
    {"name": _TAG_TASKS,   "description": "按优先级串行执行的批量生图任务队列，支持暂停 / 恢复 / 删除 / 排序。"},
    {"name": _TAG_GALLERY, "description": "会话（session）管理与图库分页浏览；图片 / 视频文件直接通过 CDN 风格 URL 访问。"},
    {"name": _TAG_VIDEO,   "description": "Grok 视频生成：提交任务获取 video_id，轮询状态直到下载链接就绪。"},
    {"name": _TAG_QUOTA,   "description": "查询 Grok 账号当前额度剩余情况。"},
    {"name": _TAG_LOGS,    "description": "聊天 / 图片请求日志的查询与清理。"},
    {"name": _TAG_ADMIN,   "description": "运行状态、配置更新、cURL 一键导入（需要管理员 API Key）。"},
]

settings_store = SettingsStore(load_settings())
monitor = Monitor()
ws_gateway = WsGateway()
image_stream_worker = ImageStreamWorker()
task_queue = TaskQueue()
log_db = LogDB()


@asynccontextmanager
async def _lifespan(app: FastAPI):  # noqa: ARG001
    import asyncio
    _key = settings_store.get().api_key
    if not _key:
        logger.warning("⚠️  api_key 为空，所有接口无需认证——仅限受信任内网使用")
    elif _key == "change-me":
        logger.warning("⚠️  api_key 使用默认值 'change-me'，请立即修改 data/config/mini.toml")
    ws_gateway.start(settings_store.get)
    task_queue.start_worker(ws_gateway, log_db=log_db)
    asyncio.create_task(_daily_cleanup())
    asyncio.create_task(_session_keeper_loop())
    yield
    ws_gateway.stop()
    task_queue.stop_worker()
    image_stream_worker.stop()
    fs_url = settings_store.get().flaresolverr_url
    if fs_url:
        await flaresolverr_destroy_session(fs_url)


app = FastAPI(
    title="xGate API",
    version="0.1.0",
    lifespan=_lifespan,
    description=(
        "**xAI Grok → OpenAI-compatible API 网关**\n\n"
        "将 Grok Web 端接口包装为标准 OpenAI 格式，支持 Chat、图片生成、连续生图、任务队列、视频生成。\n\n"
        "### 认证\n"
        "所有 `/v1/*` 和 `/admin/*` 接口均需在请求头中携带：\n"
        "```\nAuthorization: Bearer <api_key>\n```\n"
        "或 `X-Api-Key: <api_key>`。文件服务 (`/v1/files/*`) 无需认证。\n\n"
        "### 快速上手\n"
        "1. 在「设置」页面粘贴 Grok cURL 完成 Cookie 导入\n"
        "2. 使用任意 OpenAI 兼容客户端将 `base_url` 指向本服务\n"
        "3. 图片生成使用模型名 `grok-imagine`（Speed）或 `grok-imagine-pro`（Quality）"
    ),
    openapi_tags=_TAGS_META,
    docs_url="/docs",
    redoc_url="/redoc",
)
_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_DIR.mkdir(exist_ok=True)


async def _daily_cleanup() -> None:
    import asyncio
    while True:
        await asyncio.sleep(86400)
        try:
            log_db.cleanup(settings_store.get().log_retention_days)
        except Exception as exc:
            logger.warning("daily cleanup failed: %s", exc)


_KEEPER_BASE = 10 * 60   # 10 分钟
_KEEPER_JITTER = 2 * 60  # ±2 分钟随机抖动


async def _heartbeat_once(settings: Settings) -> None:
    """发送一次 /rest/skills 心跳，捕获新 __cf_bm 并回写配置。"""
    import re as _re
    from curl_cffi.requests import AsyncSession as _HBSession
    try:
        h = _headers(settings)
        h["Content-Type"] = "application/json"
        async with _HBSession(**_session_kwargs(settings)) as sess:
            resp = await sess.post(SKILLS_URL, headers=h, data=b'{"locale":"en"}', timeout=20.0)
            raw_sc = resp.headers.get("set-cookie", "")
            if "__cf_bm=" in raw_sc:
                m = _re.search(r"__cf_bm=([^;]+)", raw_sc)
                if m:
                    new_bm = m.group(1)
                    old_cookie = settings.grok_cookie
                    if "__cf_bm=" in old_cookie:
                        updated = _re.sub(r"__cf_bm=[^;]*", f"__cf_bm={new_bm}", old_cookie)
                    else:
                        updated = old_cookie.rstrip("; ") + f"; __cf_bm={new_bm}"
                    if updated != old_cookie:
                        settings_store.update(grok_cookie=updated)
                        logger.info("heartbeat: __cf_bm refreshed")
            logger.info("heartbeat: /rest/skills OK (status=%s)", resp.status_code)
    except Exception as exc:
        logger.warning("heartbeat failed: %s", exc)


async def _session_keeper_loop() -> None:
    """
    定时刷新 Grok session cookies。

    有 FLARESOLVERR_URL：调用 FlareSolverr 解 CF 拿 cf_clearance + __cf_bm + UA，
    与已有 sso/sso-rw 登录态合并写回 settings_store；无 sso 时只更新 CF 部分。
    无 FLARESOLVERR_URL：退化为 /rest/skills 心跳（仅在已有 cookie 时）。
    间隔：10 分钟 ± 2 分钟随机抖动。
    """
    import asyncio
    import random
    await asyncio.sleep(5)
    while True:
        settings = settings_store.get()
        fs_url = settings.flaresolverr_url
        if fs_url:
            try:
                # 完全不传现有 cookie 给 FlareSolverr：
                # 1. 传 sso 会触发服务端清除登录态（FlareSolverr 浏览器 IP/指纹与原浏览器不同）
                # 2. 传旧 cf_clearance 时 FlareSolverr 看到"已通过 CF"，不会发起新挑战，
                #    返回的还是旧 cookie（CF 绑定 Chrome 真实指纹，curl_cffi 用不了）
                # 让 FlareSolverr 在干净浏览器里重新解挑战，颁发与本次请求 IP/UA 配套的新 cf_clearance。
                fresh_cookies, ua = await flaresolverr_refresh_cf(
                    fs_url,
                    existing_cookies=None,
                    grok_proxy=settings.grok_proxy,
                    flaresolverr_proxy_url=settings.flaresolverr_proxy_url,
                )
                if fresh_cookies:
                    # 只合并 CF 相关 cookie。FlareSolverr 的匿名 Chrome 还会返回
                    # x-anonuserid / x-challenge / x-signature / OptanonConsent / mp_*
                    # 等"它自己 session"的元数据，与用户的登录态(x-userid/sso) 冲突，
                    # 服务端会拒绝请求。
                    cf_only_fresh = {
                        k: v for k, v in fresh_cookies.items()
                        if k in ("cf_clearance", "__cf_bm") and v
                    }
                    merged = merge_grok_cookies(settings.grok_cookie, cf_only_fresh)
                    patch: dict[str, object] = {"grok_cookie": merged}
                    if ua:
                        patch["grok_user_agent"] = ua
                        # 同步 grok_browser 与 UA 主版本号匹配，否则 curl_cffi 指纹与
                        # FlareSolverr 颁发 cf_clearance 时的 Chrome 指纹不一致 → CF 拒绝
                        import re
                        m = re.search(r"Chrome/(\d+)", ua)
                        if m:
                            major = m.group(1)
                            target_browser = f"chrome{major}"
                            # 只在 curl_cffi 实际支持的版本范围内更新（避免 fallback 失败）
                            from curl_cffi.requests.impersonate import BrowserType
                            if any(b.name == target_browser for b in BrowserType):
                                patch["grok_browser"] = target_browser
                    settings_store.update(**patch)
                    merged_names = list(parse_cookie_string(merged).keys())
                    logger.info(
                        "session_keeper: refreshed via FlareSolverr (cf_clearance=%s, sso=%s, cookies=%d)",
                        "yes" if "cf_clearance" in cf_only_fresh else "no",
                        "yes" if "sso" in merged_names else "no",
                        len(merged_names),
                    )
            except Exception as exc:
                logger.warning("session_keeper: FlareSolverr failed (%s)", exc)
                if settings.grok_cookie:
                    await _heartbeat_once(settings)
        elif settings.grok_cookie:
            await _heartbeat_once(settings)
        interval = _KEEPER_BASE + random.randint(-_KEEPER_JITTER, _KEEPER_JITTER)
        await asyncio.sleep(interval)




def _settings() -> Settings:
    return settings_store.get()


def _require_api_key(
    settings: Annotated[Settings, Depends(_settings)],
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="x-api-key")] = None,
) -> None:
    if not settings.api_key:
        logger.warning("api_key 未配置，跳过认证")
        return
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    candidates = [item for item in (bearer, x_api_key) if item]
    if not any(secrets.compare_digest(settings.api_key, item) for item in candidates):
        raise HTTPException(
            status_code=401,
            detail=error_payload("Invalid API key", error_type="authentication_error")["error"],
        )


def _extract_prompt(req: ChatCompletionRequest) -> str:
    parts: list[str] = []
    for message in req.messages:
        role = message.role
        content = message.content
        if isinstance(content, str):
            text = content.strip()
            if text:
                parts.append(f"[{role}]: {text}")
            continue
        if isinstance(content, list):
            for block in content:
                block_type = block.get("type")
                if block_type == "text":
                    text = str(block.get("text") or "").strip()
                    if text:
                        parts.append(f"[{role}]: {text}")
                elif block_type == "image_url":
                    raise ValueError("image_url input is not implemented yet")
    return "\n\n".join(parts).strip()


def _error_response(message: str, status: int, *, code: str | None = None) -> JSONResponse:
    # 上游 Grok 返回 401（未登录）不能原样返回，否则前端会误判 xgate 鉴权失效踢回登录
    # 401 的语义专门保留给 xgate 自身鉴权失败（_require_api_key 抛 HTTPException）
    if status == 401:
        status = 502
        code = code or "upstream_unauthorized"
    err_type = "invalid_request_error" if status < 500 else "server_error"
    if code in {"cloudflare_challenge", "missing_grok_cookie", "upstream_unauthorized"}:
        err_type = "upstream_error"
    return JSONResponse(
        error_payload(message, error_type=err_type, code=code),
        status_code=status,
    )


def _base_url(settings: Settings) -> str:
    host = settings.server_host if settings.server_host != "0.0.0.0" else "127.0.0.1"
    return f"http://{host}:{settings.server_port}"


# ---------------------------------------------------------------------------
# Root → serve UI
# ---------------------------------------------------------------------------

@app.get("/")
async def root() -> FileResponse:
    return FileResponse(str(_STATIC_DIR / "index.html"))


# ---------------------------------------------------------------------------
# Health / Models
# ---------------------------------------------------------------------------

@app.get("/health", tags=[_TAG_ADMIN], summary="健康检查", description="返回服务存活状态及 Cookie 配置情况，无需认证。")
async def health(settings: Annotated[Settings, Depends(_settings)]) -> dict:
    return {
        "ok": True,
        "cookie_configured": bool(settings.grok_cookie),
        "browser": settings.grok_browser,
    }


@app.get("/v1/models", tags=[_TAG_OPENAI], summary="列出所有模型",
         description="返回支持的模型列表，格式兼容 OpenAI `/v1/models`。包含聊天模型和图片生成模型。",
         dependencies=[Depends(_require_api_key)])
async def models_list() -> JSONResponse:
    return JSONResponse({"object": "list", "data": list_models()})


@app.get("/v1/models/{model_id}", tags=[_TAG_OPENAI], summary="查询单个模型",
         description="按 model_id 返回模型详情。若不存在返回 404。",
         dependencies=[Depends(_require_api_key)])
async def model_get(model_id: str) -> JSONResponse:
    spec = get_model(model_id)
    if spec is None:
        return _error_response(f"Model {model_id!r} not found", 404, code="model_not_found")
    return JSONResponse(model_to_openai(spec))


# ---------------------------------------------------------------------------
# Image serving (session-scoped)
# ---------------------------------------------------------------------------

@app.get("/v1/files/image/{session_id}/{filename}", tags=[_TAG_GALLERY],
         summary="获取图片文件", description="直接返回图片文件内容（无需认证）。URL 由图库接口提供，可直接在浏览器或 `<img>` 中使用。")
async def serve_image(session_id: str, filename: str) -> FileResponse:
    safe_sid = Path(session_id).name
    safe_fn = Path(filename).name
    path = IMAGE_DIR / safe_sid / safe_fn
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(str(path))


_VIDEO_MEDIA_TYPES = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}


@app.get("/v1/files/video/{session_id}/{filename}", tags=[_TAG_GALLERY],
         summary="获取视频文件", description="直接返回视频文件内容（无需认证）。支持 mp4 / webm / mov，返回正确的 Content-Type。")
async def serve_video(session_id: str, filename: str) -> FileResponse:
    safe_sid = Path(session_id).name
    safe_fn = Path(filename).name
    path = IMAGE_DIR / safe_sid / safe_fn
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Video not found")
    media_type = _VIDEO_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(str(path), media_type=media_type)


@app.post("/v1/videos/generate", tags=[_TAG_VIDEO], summary="提交视频生成任务",
          description=(
              "调用 Grok `POST /rest/media/post/create` 提交视频生成，立即返回 `video_id`。\n\n"
              "**轮询完成状态**：使用返回的 `video_id` 调用 `GET /v1/videos/{video_id}/status`，"
              "当 `ready=true` 时 `download_url` 包含视频下载链接。\n\n"
              "**请求体**：`prompt`（必填）、`resolution`（480p/720p）、`duration`（6s/10s）、`aspect_ratio`。"
          ),
          dependencies=[Depends(_require_api_key)])
async def videos_generate(
    req: VideoGenerationRequest,
    settings: Annotated[Settings, Depends(_settings)],
) -> JSONResponse:
    """提交 Grok 视频生成，流式等待完成后返回 video_id，前端轮询 /v1/videos/{video_id}/status。"""
    if not req.prompt.strip():
        return _error_response("prompt cannot be empty", 400, code="invalid_prompt")
    # 解析 duration 字符串（"6s" → 6）
    try:
        duration_sec = int(req.duration.rstrip("s")) if req.duration else 5
    except ValueError:
        duration_sec = 5
    monitor.record_start()
    rid = response_id()
    t0 = time.monotonic()
    prompt_text = req.prompt.strip()
    aspect_ratio = req.aspect_ratio or "16:9"
    resolution = req.resolution or "480p"
    import uuid as _uuid
    session_id = str(_uuid.uuid4())
    video_id: str | None = None
    try:
        video_id = await create_video(
            settings,
            prompt=prompt_text,
            aspect_ratio=aspect_ratio,
            duration=duration_sec,
            resolution=resolution,
            session_id=session_id,
            model_label="grok-imagine-video",
        )
    except GrokClientError as exc:
        msg = str(exc)
        if exc.body:
            logger.error("video generate error body: %s", exc.body)
            msg = f"{msg} | upstream: {exc.body}"
        monitor.record_failure(exc.status_code, str(exc), cloudflare=exc.code == "cloudflare_challenge")
        log_db.log_video(
            request_id=rid, model="grok-imagine-video", prompt=prompt_text,
            session_id=session_id, aspect_ratio=aspect_ratio,
            duration_sec=duration_sec, resolution=resolution, source="api",
            status="error", duration_ms=int((time.monotonic() - t0) * 1000), error=str(exc),
        )
        return _error_response(msg, exc.status_code, code=exc.code or "upstream_error")
    except Exception as exc:
        logger.exception("video generation failed")
        monitor.record_failure(500, str(exc))
        log_db.log_video(
            request_id=rid, model="grok-imagine-video", prompt=prompt_text,
            session_id=session_id, aspect_ratio=aspect_ratio,
            duration_sec=duration_sec, resolution=resolution, source="api",
            status="error", duration_ms=int((time.monotonic() - t0) * 1000), error=str(exc),
        )
        return _error_response("Internal server error", 500)
    monitor.record_success()
    log_db.log_video(
        request_id=rid, model="grok-imagine-video", prompt=prompt_text,
        video_path=f"data/images/{session_id}/{video_id}.mp4",
        session_id=session_id, aspect_ratio=aspect_ratio,
        duration_sec=duration_sec, resolution=resolution, source="api",
        status="success", duration_ms=int((time.monotonic() - t0) * 1000),
    )
    return JSONResponse({"ok": True, "video_id": video_id})


@app.get("/v1/videos/{video_id}/status", tags=[_TAG_VIDEO], summary="轮询视频生成状态",
         description=(
             "调用 Grok `POST /rest/media/post/create-link` 查询视频是否生成完毕。\n\n"
             "- `ready=false`：仍在生成中，建议 5 秒后重试\n"
             "- `ready=true`：返回 `download_url`，可直接下载 / 播放视频"
         ),
         dependencies=[Depends(_require_api_key)])
async def video_status(
    video_id: str,
    settings: Annotated[Settings, Depends(_settings)],
) -> JSONResponse:
    """轮询视频生成状态，ready=true 时返回 download_url。"""
    try:
        media_url = await get_video_link(settings, video_id)
    except GrokClientError as exc:
        return _error_response(str(exc), exc.status_code, code=exc.code or "upstream_error")
    except Exception as exc:
        logger.exception("video status check failed")
        return _error_response("Internal server error", 500)
    if media_url:
        return JSONResponse({"ready": True, "video_id": video_id, "download_url": media_url})
    return JSONResponse({"ready": False, "video_id": video_id})


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------

@app.post(
    "/v1/images/generations",
    tags=[_TAG_OPENAI],
    summary="一次性图片生成",
    description=(
        "兼容 OpenAI `POST /v1/images/generations`。\n\n"
        "**模型**：`grok-imagine`（Speed，快速）/ `grok-imagine-pro`（Quality，高质量）\n\n"
        "**size 与比例映射**：`1024x1024`=1:1 · `1024x1792`=2:3(竖) · `1792x1024`=3:2(横) · `1280x720`=16:9 · `720x1280`=9:16\n\n"
        "**返回**：`{\"created\": ts, \"data\": [{\"url\": \"http://...\"}]}`，URL 指向本服务文件接口。"
    ),
    dependencies=[Depends(_require_api_key)],
    response_model=None,
)
async def images_generations(
    req: ImageGenerationRequest,
    settings: Annotated[Settings, Depends(_settings)],
) -> JSONResponse:
    spec = get_model(req.model)
    if spec is None or not spec.image_model:
        return _error_response(f"Model {req.model!r} not found or not an image model", 404, code="model_not_found")
    if not req.prompt.strip():
        return _error_response("prompt cannot be empty", 400, code="invalid_prompt")

    monitor.record_start()
    prompt = req.prompt.strip()
    aspect_ratio = resolve_aspect_ratio(req.size)
    session_id = str(__import__("uuid").uuid4())
    session_dir = _init_session(session_id, prompt=prompt, source="api", aspect_ratio=aspect_ratio)
    t0 = time.monotonic()
    try:
        batch = await ws_gateway.generate_images(
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            enable_pro=spec.enable_pro,
            session_dir=session_dir,
        )
        images = sorted(batch[: req.n], key=lambda r: r.order)
    except GrokClientError as exc:
        code = exc.code or "upstream_error"
        monitor.record_failure(exc.status_code, str(exc), cloudflare=code == "cloudflare_challenge")
        log_db.log_image(
            request_id=session_id, model=req.model, prompt=prompt,
            aspect_ratio=aspect_ratio, source="api",
            status="error", duration_ms=int((time.monotonic() - t0) * 1000), error=str(exc),
        )
        return _error_response(str(exc), exc.status_code, code=code)
    except Exception as exc:
        logger.exception("image generation failed")
        monitor.record_failure(500, str(exc))
        log_db.log_image(
            request_id=session_id, model=req.model, prompt=prompt,
            aspect_ratio=aspect_ratio, source="api",
            status="error", duration_ms=int((time.monotonic() - t0) * 1000), error=str(exc),
        )
        return _error_response("Internal server error", 500)

    monitor.record_success()
    log_db.log_image(
        request_id=session_id, model=req.model, prompt=prompt,
        image_paths=[img.serve_path for img in images],
        image_count=len(images),
        aspect_ratio=aspect_ratio, source="api",
        status="success", duration_ms=int((time.monotonic() - t0) * 1000),
    )
    base = _base_url(settings)

    def _item(img: ImageResult) -> dict:
        url = f"{base}/v1/files/image/{img.serve_path}"
        return {"url": url}

    return JSONResponse({"created": int(time.time()), "data": [_item(img) for img in images]})


# ---------------------------------------------------------------------------
# Chat completions
# ---------------------------------------------------------------------------

@app.post(
    "/v1/chat/completions",
    tags=[_TAG_OPENAI],
    summary="聊天补全（Chat Completions）",
    description=(
        "兼容 OpenAI `POST /v1/chat/completions`，支持流式（`stream=true`）与非流式。\n\n"
        "**可用模型**：见 `GET /v1/models`，默认对话模型为 `grok-3`。\n\n"
        "**流式响应**：`text/event-stream`，每行格式为 `data: {...}`，结束时发送 `data: [DONE]`。\n\n"
        "**非流式响应**：标准 OpenAI ChatCompletion 对象。"
    ),
    dependencies=[Depends(_require_api_key)],
    response_model=None,
)
async def chat_completions(
    req: ChatCompletionRequest,
    settings: Annotated[Settings, Depends(_settings)],
) -> JSONResponse | StreamingResponse:
    spec = get_model(req.model)
    if spec is None:
        return _error_response(f"Model {req.model!r} not found", 404, code="model_not_found")
    if not req.messages:
        return _error_response("messages cannot be empty", 400, code="invalid_messages")
    try:
        prompt = _extract_prompt(req)
    except ValueError as exc:
        return _error_response(str(exc), 400, code="unsupported_content_type")
    if not prompt:
        return _error_response("message content cannot be empty", 400, code="empty_message")

    monitor.record_start()
    if req.stream:
        rid = response_id()
        t0 = time.monotonic()

        async def generate() -> AsyncGenerator[str, None]:
            chunks: list[str] = []
            try:
                async for delta in stream_chat(settings, message=prompt, mode_id=spec.mode_id):
                    if delta.done:
                        break
                    chunks.append(delta.content)
                    yield sse_data(stream_chunk(rid, req.model, delta.content))
                yield sse_data(stream_chunk(rid, req.model, "", finish_reason="stop"))
                yield "data: [DONE]\n\n"
                monitor.record_success()
                log_db.log_chat(
                    request_id=rid, model=req.model, prompt=prompt,
                    response="".join(chunks),
                    status="success", duration_ms=int((time.monotonic() - t0) * 1000),
                )
            except GrokClientError as exc:
                code = exc.code or "upstream_error"
                monitor.record_failure(exc.status_code, str(exc), cloudflare=code == "cloudflare_challenge")
                log_db.log_chat(
                    request_id=rid, model=req.model, prompt=prompt,
                    status="error", duration_ms=int((time.monotonic() - t0) * 1000), error=str(exc),
                )
                yield sse_error(str(exc), error_type="upstream_error", code=code)
                yield "data: [DONE]\n\n"
            except Exception as exc:
                monitor.record_failure(500, str(exc))
                log_db.log_chat(
                    request_id=rid, model=req.model, prompt=prompt,
                    status="error", duration_ms=int((time.monotonic() - t0) * 1000), error=str(exc),
                )
                yield sse_error(str(exc))
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    rid = response_id()
    t0 = time.monotonic()
    try:
        content = await complete_chat(settings, message=prompt, mode_id=spec.mode_id)
    except GrokClientError as exc:
        code = exc.code or "upstream_error"
        monitor.record_failure(exc.status_code, str(exc), cloudflare=code == "cloudflare_challenge")
        log_db.log_chat(
            request_id=rid, model=req.model, prompt=prompt,
            status="error", duration_ms=int((time.monotonic() - t0) * 1000), error=str(exc),
        )
        return _error_response(str(exc), exc.status_code, code=code)
    except Exception as exc:
        logger.exception("chat completion failed")
        monitor.record_failure(500, str(exc))
        log_db.log_chat(
            request_id=rid, model=req.model, prompt=prompt,
            status="error", duration_ms=int((time.monotonic() - t0) * 1000), error=str(exc),
        )
        return _error_response("Internal server error", 500)

    monitor.record_success()
    log_db.log_chat(
        request_id=rid, model=req.model, prompt=prompt, response=content,
        status="success", duration_ms=int((time.monotonic() - t0) * 1000),
    )
    return JSONResponse(chat_response(req.model, content, prompt))


# ---------------------------------------------------------------------------
# Image stream worker
# ---------------------------------------------------------------------------

@app.post("/v1/images/stream/start", tags=[_TAG_STREAM], summary="启动连续生图",
          description=(
              "启动后台持续生图 worker。同一时刻只允许一个 worker 运行，重复调用返回 409。\n\n"
              "**参数**：`prompt`、`model`、`size`、`interval_seconds`（批次间隔秒数）、"
              "`max_rounds`（最大批次，-1=不限）、`image_data`（可选参考图，base64 DataURL）。\n\n"
              "**返回**：`session_id` 用于轮询图库；用 `GET /v1/images/stream/status` 查看进度。"
          ),
          dependencies=[Depends(_require_api_key)])
async def stream_start(req: ImageStreamStartRequest) -> JSONResponse:
    spec = get_model(req.model)
    if spec is None or not spec.image_model:
        return _error_response(f"Model {req.model!r} not found or not an image model", 404, code="model_not_found")
    if not req.prompt.strip():
        return _error_response("prompt cannot be empty", 400, code="invalid_prompt")
    if image_stream_worker.is_running():
        return JSONResponse({"ok": False, "message": "worker already running"}, status_code=409)
    cfg = StreamConfig(
        prompt=req.prompt.strip(),
        model=req.model,
        n=req.n,
        size=req.size,
        interval_seconds=req.interval_seconds,
        max_rounds=req.max_rounds,
        enable_pro=spec.enable_pro,
        image_data=req.image_data or None,
    )
    session_id = image_stream_worker.start(ws_gateway, cfg, log_db=log_db)
    return JSONResponse({
        "ok": True,
        "message": "stream started",
        "session_id": session_id,
        "config": image_stream_worker.status()["config"],
    })


@app.post("/v1/images/stream/stop", tags=[_TAG_STREAM], summary="停止连续生图",
          description="向 worker 发送停止信号，当前批次完成后退出。若 worker 未运行返回 409。",
          dependencies=[Depends(_require_api_key)])
async def stream_stop() -> JSONResponse:
    if not image_stream_worker.is_running():
        return JSONResponse({"ok": False, "message": "worker not running"}, status_code=409)
    image_stream_worker.stop()
    return JSONResponse({"ok": True, "message": "stop signal sent"})


@app.get("/v1/images/stream/status", tags=[_TAG_STREAM], summary="查询连续生图状态",
         description="返回 worker 运行状态：`running`、`session_id`、`success_count`、`current_round`、配置快照等。",
         dependencies=[Depends(_require_api_key)])
async def stream_status() -> JSONResponse:
    return JSONResponse(image_stream_worker.status())


# ---------------------------------------------------------------------------
# Task queue
# ---------------------------------------------------------------------------

@app.get("/v1/images/tasks", tags=[_TAG_TASKS], summary="列出任务队列",
         description="返回所有任务（按优先级排序）及统计摘要（各状态计数）。",
         dependencies=[Depends(_require_api_key)])
async def task_list() -> JSONResponse:
    return JSONResponse({
        "tasks": task_queue.list_tasks(),
        "stats": task_queue.stats(),
    })


@app.post("/v1/images/tasks", tags=[_TAG_TASKS], summary="新建生图任务",
          description=(
              "向队列添加一个批量生图任务。\n\n"
              "**参数**：`prompt`、`model`、`target_count`（目标图片数）、`size`（比例）、`interval_seconds`（批次间隔）。\n\n"
              "**返回**：201 + 任务详情（含 `id` 用于后续管理）。任务按优先级追加到末尾。"
          ),
          dependencies=[Depends(_require_api_key)])
async def task_add(
    req: TaskQueueAddRequest,
    settings: Annotated[Settings, Depends(_settings)],
) -> JSONResponse:
    spec = get_model(req.model)
    if spec is None or not spec.image_model:
        return _error_response(f"Model {req.model!r} not found or not an image model", 404, code="model_not_found")
    if not req.prompt.strip():
        return _error_response("prompt cannot be empty", 400, code="invalid_prompt")
    task = await task_queue.add_task(
        prompt=req.prompt.strip(),
        target_count=req.target_count,
        aspect_ratio=resolve_aspect_ratio(req.size),
        enable_pro=spec.enable_pro,
        interval_seconds=req.interval_seconds,
    )
    return JSONResponse(task.to_dict(), status_code=201)


@app.get("/v1/images/tasks/{task_id}", tags=[_TAG_TASKS], summary="查询单个任务",
         description="返回指定任务的详情，包括进度（`generated_count` / `target_count`）和当前状态。",
         dependencies=[Depends(_require_api_key)])
async def task_get(task_id: str) -> JSONResponse:
    t = task_queue.get_task(task_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return JSONResponse(t)


@app.delete("/v1/images/tasks/{task_id}", tags=[_TAG_TASKS], summary="删除任务",
            description="取消并从队列中移除任务。若任务正在运行，先发送停止信号再删除。",
            dependencies=[Depends(_require_api_key)])
async def task_delete(task_id: str) -> JSONResponse:
    ok = await task_queue.remove_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Task not found")
    return JSONResponse({"ok": True})


@app.post("/v1/images/tasks/{task_id}/pause", tags=[_TAG_TASKS], summary="暂停任务",
          description="暂停 running 或 pending 状态的任务。若任务正在生图，当前批次完成后停止。",
          dependencies=[Depends(_require_api_key)])
async def task_pause(task_id: str) -> JSONResponse:
    ok = await task_queue.pause_task(task_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Task cannot be paused in its current state")
    return JSONResponse({"ok": True})


@app.post("/v1/images/tasks/{task_id}/resume", tags=[_TAG_TASKS], summary="恢复任务",
          description="将 paused 状态的任务恢复为 pending，下一个 worker 调度周期内开始执行。",
          dependencies=[Depends(_require_api_key)])
async def task_resume(task_id: str) -> JSONResponse:
    ok = await task_queue.resume_task(task_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Task cannot be resumed in its current state")
    return JSONResponse({"ok": True})


@app.post("/v1/images/tasks/{task_id}/move", tags=[_TAG_TASKS], summary="调整任务优先级",
          description="将任务在队列中上移（`?direction=up`）或下移（`?direction=down`）一位。",
          dependencies=[Depends(_require_api_key)])
async def task_move(task_id: str, direction: Literal["up", "down"] = "up") -> JSONResponse:
    ok = await task_queue.move_task(task_id, direction)
    if not ok:
        raise HTTPException(status_code=409, detail="Cannot move task")
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Gallery & Sessions
# ---------------------------------------------------------------------------

@app.get("/v1/images/sessions", tags=[_TAG_GALLERY], summary="列出所有会话",
         description=(
             "按修改时间倒序返回所有 session 列表，含提示词、缩略图文件名、图片数量、创建时间。\n\n"
             "隐藏（软删除）的 session 不会出现在列表中。"
         ),
         dependencies=[Depends(_require_api_key)])
async def sessions_list() -> JSONResponse:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    # 从 DB 批量查 session_id → model 映射（每个 session 取第一条记录的 model）
    model_map: dict[str, str] = {}
    if log_db:
        try:
            with log_db._connect() as conn:
                rows = conn.execute(
                    "SELECT request_id, model FROM image_logs GROUP BY request_id"
                ).fetchall()
                model_map = {r[0]: r[1] for r in rows}
        except Exception:
            pass
    sessions = []
    for d in sorted(IMAGE_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        if (d / ".hidden").exists():
            continue
        meta_file = d / "session.json"
        meta: dict = {}
        if meta_file.exists():
            try:
                raw = meta_file.read_bytes()
                meta = json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:
                pass
        images = sorted(
            [f for f in d.iterdir() if f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png", ".mp4", ".webm", ".mov"}],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        sessions.append({
            "session_id": d.name,
            "prompt": meta.get("prompt", ""),
            "source": meta.get("source", ""),
            "aspect_ratio": meta.get("aspect_ratio", ""),
            "model": model_map.get(d.name, ""),
            "created_at": meta.get("created_at", d.stat().st_mtime),
            "image_count": len(images),
            "thumbnail": images[0].name if images else None,
        })
    return JSONResponse({"sessions": sessions, "total": len(sessions)})


@app.delete("/v1/images/sessions/{session_id}", tags=[_TAG_GALLERY], summary="删除 / 隐藏会话",
            description=(
                "- `delete_files=false`（默认）：软删除，创建 `.hidden` 标记文件，不删除图片\n"
                "- `delete_files=true`：彻底删除整个 session 目录及所有图片文件"
            ),
            dependencies=[Depends(_require_api_key)])
async def session_delete(session_id: str, delete_files: bool = False) -> JSONResponse:
    import shutil
    safe = Path(session_id).name
    d = IMAGE_DIR / safe
    if not d.is_dir():
        raise HTTPException(status_code=404, detail="Session not found")
    if delete_files:
        shutil.rmtree(str(d), ignore_errors=True)
    else:
        (d / ".hidden").touch()
    return JSONResponse({"ok": True, "deleted_files": delete_files})


@app.get("/v1/images/gallery", tags=[_TAG_GALLERY], summary="图库分页浏览",
         description=(
             "分页返回图片 / 视频文件列表，每项含 `url`、`file_type`（image/video）、`session_id`、`size_bytes`。\n\n"
             "**参数**：`session_id`（可选，过滤单个会话）、`offset`、`limit`（默认 40）。\n\n"
             "文件 URL 可直接在浏览器访问（无需认证）。"
         ),
         dependencies=[Depends(_require_api_key)])
async def gallery(
    session_id: str | None = None,
    offset: int = 0,
    limit: int = 40,
    settings: Annotated[Settings, Depends(_settings)] = None,
) -> JSONResponse:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    base = _base_url(settings or settings_store.get())

    _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    _VIDEO_EXTS = {".mp4", ".webm", ".mov"}

    def _collect_from_dir(d: Path) -> list[dict]:
        files = sorted(
            [f for f in d.iterdir() if f.is_file() and f.suffix.lower() in _IMAGE_EXTS | _VIDEO_EXTS],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        sid = d.name
        result = []
        for f in files:
            is_vid = f.suffix.lower() in _VIDEO_EXTS
            result.append({
                "session_id": sid,
                "filename": f.name,
                "file_type": "video" if is_vid else "image",
                "url": f"{base}/v1/files/{'video' if is_vid else 'image'}/{sid}/{f.name}",
                "size_bytes": f.stat().st_size,
                "created_at": f.stat().st_mtime,
            })
        return result

    if session_id:
        d = IMAGE_DIR / Path(session_id).name
        if not d.is_dir():
            return JSONResponse({"total": 0, "offset": offset, "limit": limit, "data": []})
        all_items = _collect_from_dir(d)
    else:
        all_items = []
        for d in IMAGE_DIR.iterdir():
            if d.is_dir() and not (d / ".hidden").exists():
                all_items.extend(_collect_from_dir(d))
        # 全局按文件时间倒序，避免按 session 分组导致 CSS columns 竖排
        all_items.sort(key=lambda x: x["created_at"], reverse=True)

    total = len(all_items)
    return JSONResponse({
        "total": total,
        "offset": offset,
        "limit": limit,
        "data": all_items[offset: offset + limit],
    })


# ---------------------------------------------------------------------------
# Quota check
# ---------------------------------------------------------------------------

_RATE_LIMITS_URL = "https://grok.com/rest/rate-limits"
_QUOTA_MODES = {
    "auto": 0,
    "fast": 1,
    "think": 2,
}

# Image generation uses the "auto" quota bucket
_IMAGE_QUOTA_MODE = "auto"


async def _fetch_quota(settings: Settings, mode_name: str = "auto") -> dict | None:
    """POST /rest/rate-limits for a mode, return parsed quota or None."""
    import aiohttp
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                _RATE_LIMITS_URL,
                headers={**_headers(settings), "Content-Type": "application/json"},
                json={"modelName": mode_name},
                timeout=aiohttp.ClientTimeout(total=20),
                ssl=False,
            ) as resp:
                if resp.status != 200:
                    return None
                body = await resp.json(content_type=None)
        remaining = body.get("remainingQueries")
        if remaining is None:
            return None
        total = body.get("totalQueries") or remaining
        window = body.get("windowSizeSeconds") or 72000
        used = total - remaining
        pct = round(used / total * 100, 1) if total > 0 else 0.0
        return {
            "mode": mode_name,
            "remaining": int(remaining),
            "total": int(total),
            "used": int(used),
            "used_pct": pct,
            "window_seconds": int(window),
        }
    except Exception as exc:
        logger.warning("quota fetch failed: mode=%s error=%s", mode_name, exc)
        return None


@app.get("/v1/quota", tags=[_TAG_QUOTA], summary="查询额度剩余",
         description=(
             "并发请求 Grok `/rest/rate-limits` 查询 auto / fast / think 三种模式的额度。\n\n"
             "**返回**：每个模式的 `remaining`、`total`、`used`、`used_pct`（百分比）、`window_seconds`（窗口时长）。\n\n"
             "当 auto 模式使用率 ≥ 90% 时 `image_blocked=true`，建议暂停生图。"
         ),
         dependencies=[Depends(_require_api_key)])
async def quota_check(settings: Annotated[Settings, Depends(_settings)]) -> JSONResponse:
    if not settings.grok_cookie:
        return _error_response("GROK_COOKIE not configured", 400, code="missing_grok_cookie")

    import asyncio
    results = await asyncio.gather(*[
        _fetch_quota(settings, mode) for mode in _QUOTA_MODES
    ])
    quotas = {r["mode"]: r for r in results if r}

    # 用 auto 模式判断是否超限 90%
    auto = quotas.get("auto")
    image_blocked = False
    if auto and auto["total"] > 0:
        image_blocked = auto["used_pct"] >= 90.0

    return JSONResponse({
        "quotas": quotas,
        "image_blocked": image_blocked,
        "blocked_reason": "quota ≥ 90%" if image_blocked else None,
    })


# ---------------------------------------------------------------------------
# Admin API (consumed by the Settings tab in the UI)
# ---------------------------------------------------------------------------

@app.post("/admin/import-curl", tags=[_TAG_ADMIN], summary="导入 cURL（一键配置）",
          description=(
              "将 Chrome DevTools 中复制的 Grok cURL 命令粘贴后自动解析 Cookie、User-Agent、浏览器指纹，"
              "并通过冒烟请求（`/rest/skills`）验证有效性后写入配置。\n\n"
              "**Content-Type**: `application/x-www-form-urlencoded`，字段名 `curl_text`。"
          ),
          dependencies=[Depends(_require_api_key)])
async def import_curl(curl_text: Annotated[str, Form()]) -> JSONResponse:
    try:
        result = parse_grok_curl(curl_text)
    except CurlImportError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    current = settings_store.get()
    candidate = replace(
        current,
        grok_cookie=result.cookie,
        grok_user_agent=result.user_agent,
        grok_browser=result.browser,
    )
    monitor.record_start()
    try:
        smoke = await smoke_skills(candidate, extra_headers=result.headers)
    except GrokClientError as exc:
        code = exc.code or "upstream_error"
        monitor.record_failure(
            exc.status_code,
            str(exc),
            cloudflare=code == "cloudflare_challenge",
        )
        return JSONResponse(
            {"ok": False, "error": f"cURL 解析成功，但 /rest/skills 冒烟失败：{exc}"},
            status_code=400,
        )

    settings_store.update(
        grok_cookie=result.cookie,
        grok_user_agent=result.user_agent,
        grok_browser=result.browser,
    )
    monitor.record_success(int(smoke["status_code"]))
    return JSONResponse({"ok": True, "message": f"cURL 导入和冒烟成功，状态码 {smoke['status_code']}。"})


@app.post("/admin/config", tags=[_TAG_ADMIN], summary="更新运行配置",
          description=(
              "逐字段更新配置（留空字段不修改）。`Content-Type: application/x-www-form-urlencoded`。\n\n"
              "**可更新字段**：`api_key`、`grok_cookie`、`grok_user_agent`、`grok_browser`（curl_cffi 指纹）、`grok_proxy`、`log_retention_days`。"
          ),
          dependencies=[Depends(_require_api_key)])
async def update_config(
    api_key: Annotated[str, Form()] = "",
    grok_cookie: Annotated[str, Form()] = "",
    grok_user_agent: Annotated[str, Form()] = "",
    grok_browser: Annotated[str, Form()] = "",
    grok_proxy: Annotated[str, Form()] = "",
    log_retention_days: Annotated[str, Form()] = "",
) -> JSONResponse:
    patch: dict[str, object] = {}
    if api_key.strip():
        patch["api_key"] = api_key.strip()
    if grok_cookie.strip():
        patch["grok_cookie"] = grok_cookie.strip()
    if grok_user_agent.strip():
        patch["grok_user_agent"] = grok_user_agent.strip()
    if grok_browser.strip():
        patch["grok_browser"] = grok_browser.strip()
    if grok_proxy.strip():
        patch["grok_proxy"] = grok_proxy.strip()
    if log_retention_days.strip():
        try:
            patch["log_retention_days"] = int(log_retention_days.strip())
        except ValueError:
            pass
    if patch:
        settings_store.update(**patch)
    return JSONResponse({"ok": True, "message": "配置已更新。"})


# ---------------------------------------------------------------------------
# Logs API
# ---------------------------------------------------------------------------

@app.get("/v1/logs/stats", tags=[_TAG_LOGS], summary="日志统计摘要",
         description="返回聊天与图片请求的汇总统计：总数、成功/失败数、近 7 天数量、近 14 天按日明细（用于折线图）。",
         dependencies=[Depends(_require_api_key)])
async def logs_stats() -> JSONResponse:
    return JSONResponse(log_db.stats())


@app.get("/v1/logs", tags=[_TAG_LOGS], summary="查询请求日志",
         description=(
             "分页查询聊天与图片日志，支持全文检索和时间范围过滤。\n\n"
             "**参数**：`log_type`（all/chat/image）、`search`（prompt/model 模糊搜索）、"
             "`offset`、`limit`（默认 50）、`from_ts` / `to_ts`（Unix 时间戳）。"
         ),
         dependencies=[Depends(_require_api_key)])
async def logs_query(
    log_type: str = "all",
    search: str = "",
    offset: int = 0,
    limit: int = 50,
    from_ts: float | None = None,
    to_ts: float | None = None,
) -> JSONResponse:
    rows, total = log_db.query(
        log_type=log_type, search=search, offset=offset, limit=limit,
        from_ts=from_ts, to_ts=to_ts,
    )
    return JSONResponse({"total": total, "offset": offset, "limit": limit, "data": rows})


@app.post("/admin/cleanup-logs", tags=[_TAG_ADMIN], summary="清理过期日志",
          description="删除超过 `log_retention_days`（默认 90 天）的历史日志记录，返回删除条数。",
          dependencies=[Depends(_require_api_key)])
async def cleanup_logs(settings: Annotated[Settings, Depends(_settings)]) -> JSONResponse:
    deleted = log_db.cleanup(settings.log_retention_days)
    return JSONResponse({"ok": True, "deleted": deleted})


@app.get("/admin/status", tags=[_TAG_ADMIN], summary="运行状态快照",
         description="返回请求计数器快照（总数 / 成功 / 失败 / CF 拦截次数）及当前配置摘要（Cookie 掩码、浏览器指纹、代理状态）。",
         dependencies=[Depends(_require_api_key)])
async def admin_status(settings: Annotated[Settings, Depends(_settings)]) -> dict:
    snapshot = monitor.snapshot()
    return {
        **asdict(snapshot),
        "cookie_configured": bool(settings.grok_cookie),
        "cookie_masked": mask_secret(settings.grok_cookie),
        "browser": settings.grok_browser,
        "proxy_configured": bool(settings.grok_proxy),
        "log_retention_days": settings.log_retention_days,
    }




_TAG_FILES = "Grok Files"


@app.get("/v1/grok/assets", tags=[_TAG_FILES], summary="列出 Grok Files",
         description="分页拉取 Grok 云端文件列表（图片/视频）。",
         dependencies=[Depends(_require_api_key)])
async def grok_assets_list(
    settings: Annotated[Settings, Depends(_settings)],
    page_token: str = "",
    page_size: int = 50,
) -> JSONResponse:
    try:
        data = await list_grok_assets(settings, page_token=page_token, page_size=min(page_size, 100))
        return JSONResponse(data)
    except GrokClientError as exc:
        return _error_response(str(exc), exc.status_code, code=exc.code or "upstream_error")
    except Exception:
        logger.exception("grok assets list failed")
        return _error_response("Internal server error", 500)


@app.post("/v1/grok/assets/{asset_id}/delete", tags=[_TAG_FILES], summary="删除 Grok File",
          description="从 Grok 云端删除指定文件。",
          dependencies=[Depends(_require_api_key)])
async def grok_asset_delete(
    asset_id: str,
    settings: Annotated[Settings, Depends(_settings)],
) -> JSONResponse:
    try:
        await delete_grok_asset(settings, asset_id)
        return JSONResponse({"ok": True})
    except GrokClientError as exc:
        return _error_response(str(exc), exc.status_code, code=exc.code or "upstream_error")
    except Exception:
        logger.exception("grok asset delete failed")
        return _error_response("Internal server error", 500)


@app.get("/v1/grok/assets/download", tags=[_TAG_FILES], summary="下载 Grok File",
         description="通过服务端代理从 assets.grok.com 下载文件，触发浏览器保存。",
         dependencies=[Depends(_require_api_key)])
async def grok_asset_download(
    settings: Annotated[Settings, Depends(_settings)],
    key: str = "",
    filename: str = "file",
) -> StreamingResponse:
    if not key:
        return JSONResponse({"error": "key is required"}, status_code=400)
    try:
        content_type, gen = await stream_grok_asset(settings, key)
        safe_name = filename.replace('"', "_")
        return StreamingResponse(
            gen,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
        )
    except GrokClientError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
    except Exception:
        logger.exception("grok asset download failed")
        return JSONResponse({"error": "Internal server error"}, status_code=500)


class _SaveBatchItem(BaseModel):
    key: str
    filename: str


class _SaveBatchRequest(BaseModel):
    files: list[_SaveBatchItem]


@app.post("/v1/grok/assets/save-local", tags=[_TAG_FILES], summary="批量下载 Grok Files 到本地",
          description="将 Grok 云端文件下载到服务器 data/grok-files/ 目录。",
          dependencies=[Depends(_require_api_key)])
async def grok_assets_save_local(
    req: _SaveBatchRequest,
    settings: Annotated[Settings, Depends(_settings)],
) -> JSONResponse:
    saved = []
    errors = []
    for item in req.files:
        try:
            path = await save_grok_asset_local(settings, item.key, item.filename)
            saved.append({"key": item.key, "filename": item.filename, "path": path})
        except GrokClientError as exc:
            errors.append({"key": item.key, "filename": item.filename, "error": str(exc)})
        except Exception as exc:
            logger.exception("save local asset failed: key=%s", item.key)
            errors.append({"key": item.key, "filename": item.filename, "error": "Internal server error"})
    return JSONResponse({"saved": saved, "errors": errors})


def run() -> None:
    import uvicorn

    settings = settings_store.get()
    uvicorn.run(
        "mini_grok_api.main:app",
        host=settings.server_host,
        port=settings.server_port,
        reload=False,
    )


if __name__ == "__main__":
    run()
