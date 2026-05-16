"""FastAPI 入口。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import sys
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import asdict, replace
from pathlib import Path
from typing import Annotated, Any, Literal

from contextlib import asynccontextmanager

from fastapi import Cookie, Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from pydantic import BaseModel

from .accounts import AccountPool, AccountAcquisition  # noqa: F401 (AccountAcquisition for type hints)
from .auth_session import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, session_store
from .config import Settings, SettingsStore, load_settings, mask_secret
from .signed_url import sign_file_url, verify_signed_path
from .db import log_db
from .accounts import Account, AccountPool, account_pool
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
    chat_imagine,
    query_rate_limits,
    query_image_rate_limits,
)
from .image_stream import ImageStreamWorker, StreamConfig
from .models import get_model, get_model_specs, list_models, model_to_openai, set_models, ModelSpec
from .monitor import Monitor
from .openai_compat import (
    chat_response,
    chat_response_tool_call,
    count_tokens,
    error_payload,
    parse_tool_call,
    response_id,
    sse_data,
    sse_error,
    stream_chunk,
    stream_usage_chunk,
    type_for_status,
)
from .schemas import ChatCompletionRequest, ImageGenerationRequest, ImageStreamStartRequest, TaskQueueAddRequest, VideoGenerationRequest
from .task_queue import TaskQueue
from .ws_gateway import WsGateway
from .mcp_server import mcp, create_mcp_app, create_sse_app, request_base_url as _mcp_request_base_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


class _SecretsRedactFilter(logging.Filter):
    """access log 脱敏：把 ?api_key= / &api_key= / Authorization 头 / x-api-key 头里的值替换成 ***。

    主要覆盖 uvicorn.access logger 与本服务自己的 logger，防止历史前端 / 老脚本
    把 api_key 拼在 URL 里时漏到 stdout。
    """

    _PATTERNS = (
        re.compile(r"([?&]api_key=)[^&\s\"']+", re.IGNORECASE),
        re.compile(r"(authorization:\s*bearer\s+)\S+", re.IGNORECASE),
        re.compile(r"(x-api-key:\s*)\S+", re.IGNORECASE),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        original = msg
        for pat in self._PATTERNS:
            msg = pat.sub(r"\1***", msg)
        if msg != original:
            record.msg = msg
            record.args = ()
        return True


for _name in ("uvicorn.access", "uvicorn", __name__):
    logging.getLogger(_name).addFilter(_SecretsRedactFilter())

_TAG_OPENAI = "OpenAI 兼容"
_TAG_STREAM = "连续生图"
_TAG_TASKS  = "任务队列"
_TAG_GALLERY = "图库 & 会话"
_TAG_VIDEO  = "视频生成"
_TAG_QUOTA  = "额度查询"
_TAG_LOGS   = "请求日志"
_TAG_ADMIN  = "管理 & 配置"
_TAG_FILES  = "Grok Files"

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

# ---------------------------------------------------------------------------
# 签名 URL 控制
# ---------------------------------------------------------------------------
# 过渡期：False = 旧 URL（无签名）仍放行（warning），True = 严格要求签名（将来切换）
_SIGNED_URL_ENFORCED = False


def _sign_file_url(path: str) -> str:
    """用当前 settings.api_key 对文件路径签名，返回带签名的完整路径。"""
    return sign_file_url(path, settings_store.get().api_key)


def _apply_models(settings: Settings) -> None:
    """从 settings.chat_models 全量加载模型注册表。"""
    specs = [
        ModelSpec(
            model_id=m["id"],
            mode_id=m["mode_id"],
            name=m.get("name", m["id"]),
            image_model=bool(m.get("image_model", False)),
            enable_pro=bool(m.get("enable_pro", False)),
        )
        for m in settings.chat_models
        if isinstance(m, dict) and m.get("id") and m.get("mode_id")
    ]
    set_models(specs)

_apply_models(settings_store.get())

monitor = Monitor()
ws_gateway = WsGateway()
image_stream_worker = ImageStreamWorker()
task_queue = TaskQueue()
# account_pool 单例由 accounts 模块持有，此处不再重复构造（避免命名空间分裂）
_mcp_app = create_mcp_app(settings_store.get)


_LOCALHOST_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

# SECURE_COOKIE_FLAG：模块加载时一次性确定，避免每请求抖动。
# 规则：always→True, never→False, auto→public_base_url 以 https:// 开头时 True。
def _compute_secure_cookie_flag(s: "Settings") -> bool:
    mode = (s.cookie_secure or "auto").strip().lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    # auto
    return s.public_base_url.startswith("https://")

SECURE_COOKIE_FLAG: bool = _compute_secure_cookie_flag(settings_store.get())


@asynccontextmanager
async def _lifespan(app: FastAPI):  # noqa: ARG001
    import asyncio
    _s = settings_store.get()
    _key = _s.api_key
    _is_weak_key = not _key or _key == "change-me"
    _is_public_host = _s.server_host not in _LOCALHOST_HOSTS
    if _is_weak_key and _is_public_host:
        logger.error(
            "启动中止：api_key 未设置或仍为默认值 'change-me'，"
            "且服务监听在公网地址 %s。"
            "请在 data/config/mini.toml 的 [auth] 节下将 api_key 改为随机强密钥后重启。",
            _s.server_host,
        )
        raise SystemExit(1)
    if not _key:
        logger.warning(
            "⚠️  api_key 为空，所有接口无需认证——仅限受信任内网使用。"
            "如需启用认证请在 data/config/mini.toml 的 [auth] 节下设置 api_key。"
        )
    elif _key == "change-me":
        logger.warning(
            "⚠️  api_key 仍为默认值 'change-me'，请在 data/config/mini.toml 的 [auth] 节下修改 api_key 后重启。"
        )
    account_pool.import_from_settings(_s)
    logger.info("account pool initialized: %d accounts", len(account_pool.list_accounts()))
    ws_gateway.start(settings_store.get, account_pool=account_pool)
    task_queue.start_worker(ws_gateway, log_db=log_db)
    log_db.reset_running_downloads()
    log_db.reset_running_deletes()
    asyncio.create_task(_daily_cleanup())
    asyncio.create_task(_session_keeper_loop())
    asyncio.create_task(_file_download_worker())
    asyncio.create_task(_file_delete_worker())
    asyncio.create_task(_files_auto_sync_loop())
    asyncio.create_task(_account_quota_poll_loop())
    asyncio.create_task(_account_revalidate_loop())
    if settings_store.get().mcp_enabled:
        async with mcp.session_manager.run():
            yield
    else:
        yield
    ws_gateway.stop()
    task_queue.stop_worker()
    image_stream_worker.stop()
    fs_url = settings_store.get().flaresolverr_url
    if fs_url:
        await flaresolverr_destroy_session(fs_url)


app = FastAPI(
    title="xGate API",
    version="0.2.0",
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
    # 禁用默认匿名 /docs / /redoc / /openapi.json；下面注册带 _require_api_key 鉴权的自定义版本
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_DIR.mkdir(exist_ok=True)


@app.middleware("http")
async def _sliding_session_middleware(request: Request, call_next):
    """响应阶段：若 _require_api_key 续期了 session，重发 Set-Cookie 同步浏览器 max_age。"""
    response = await call_next(request)
    renewed: "Session | None" = getattr(request.state, "session_renewed", None)
    if renewed is not None:
        remaining = renewed.expires_at - time.time()
        max_age = max(int(remaining), 0)
        _set_session_cookies(response, request, renewed.token, renewed.csrf, max_age)
    return response


class _MCPAwareApp:
    """Top-level ASGI: /mcp paths → MCP handler (strips prefix, no 307 redirect).
    /mcp/sse + /mcp/messages → SSE transport (for mcp-remote / stdio bridge clients).
    /mcp → Streamable HTTP transport (Claude Code, native MCP 2025-06-18 clients).
    All other scopes (incl. lifespan) → FastAPI app.
    """

    def __init__(self, fastapi_app: Any, mcp_app: Any, sse_app: Any) -> None:
        self._fastapi = fastapi_app
        self._mcp = mcp_app
        self._sse = sse_app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") in ("http", "websocket"):
            path = scope.get("path", "")
            if path == "/mcp/sse" or path.startswith("/mcp/sse/") or path.startswith("/mcp/messages"):
                new_scope = dict(scope)
                new_scope["path"] = path[4:]  # /mcp/sse → /sse, /mcp/messages → /messages
                new_scope["root_path"] = scope.get("root_path", "") + "/mcp"
                token = _mcp_request_base_url.set(self._extract_base_url(scope))
                try:
                    await self._sse(new_scope, receive, send)
                finally:
                    _mcp_request_base_url.reset(token)
                return
            if path == "/mcp" or path.startswith("/mcp/"):
                new_scope = dict(scope)
                new_scope["path"] = path[4:] or "/"
                new_scope["root_path"] = scope.get("root_path", "") + "/mcp"
                token = _mcp_request_base_url.set(self._extract_base_url(scope))
                try:
                    await self._mcp(new_scope, receive, send)
                finally:
                    _mcp_request_base_url.reset(token)
                return
        await self._fastapi(scope, receive, send)

    @staticmethod
    def _extract_base_url(scope: Any) -> str:
        headers = dict(scope.get("headers", []))
        host = headers.get(b"host", b"").decode()
        if not host:
            return ""
        scheme = "https" if scope.get("scheme") == "https" else "http"
        return f"{scheme}://{host}"


if settings_store.get().mcp_enabled:
    _sse_mcp_app = create_sse_app()
    _top_app: Any = _MCPAwareApp(app, _mcp_app, _sse_mcp_app)
    logger.info("MCP server mounted at /mcp (Streamable HTTP) + /mcp/sse (SSE), 9 tools")
else:
    _top_app = app


_FILE_DL_CONCURRENCY = 3
_file_dl_wake = asyncio.Event()
_FILE_DEL_CONCURRENCY = 2
_file_del_wake = asyncio.Event()


async def _file_download_worker() -> None:
    """后台持续消费 file_downloads 队列，并发 _FILE_DL_CONCURRENCY 个下载。"""
    import asyncio
    logger.info("file download worker started")
    while True:
        jobs = log_db.claim_pending_downloads(_FILE_DL_CONCURRENCY)
        if not jobs:
            _file_dl_wake.clear()
            try:
                # 10s 自动唤醒一次，让 retrying 到期的任务能被领走
                await asyncio.wait_for(_file_dl_wake.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                pass
            continue

        async def _do(job: dict) -> None:
            with account_pool.acquire() as _dl_acq:
                try:
                    path, _ = await save_grok_asset_local(
                        _dl_acq.settings, job["asset_key"], job["filename"], job["size_bytes"]
                    )
                    log_db.finish_download(job["id"], path=path)
                    # 同步标记 grok_assets 表中的下载状态（filename 可能含或不含扩展名）
                    fn = job["filename"]
                    asset_id = fn.rsplit(".", 1)[0] if "." in fn else fn
                    if asset_id:
                        log_db.mark_asset_downloaded(asset_id, path)
                except Exception as exc:
                    err_str = str(exc)
                    fn = job["filename"]
                    asset_id = fn.rsplit(".", 1)[0] if "." in fn else fn
                    # 404 / 410 → 立即永久失败
                    hard_perm = ("returned 404" in err_str or "returned 410" in err_str)
                    # 空 body → 视 asset 年龄而定：>24h 视为永久；<24h 让它继续重试（CDN 可能还在生成）
                    empty_err = ("empty_file" in err_str or "Asset download returned empty file" in err_str)
                    permanent = hard_perm
                    if empty_err and not permanent:
                        # 查 asset 创建时间（直接 SQL 比 list_grok_assets_db 快）
                        try:
                            with log_db._connect() as _c:
                                r = _c.execute("SELECT create_time FROM grok_assets WHERE asset_id=?", (asset_id,)).fetchone()
                                ct = r["create_time"] if r else ""
                            if ct:
                                from datetime import datetime, timezone
                                try:
                                    # Grok 时间格式: 2026-04-28T03:41:08.260755Z
                                    dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                                    age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
                                    if age_hours > 24:
                                        permanent = True  # 老 asset + 空 body → 已确认丢失
                                except Exception:
                                    pass
                        except Exception:
                            pass
                    log_db.finish_download(
                        job["id"], error=err_str, permanent=permanent,
                        max_attempts=8 if empty_err else 5,  # 空 body 给更多机会
                        base_backoff_seconds=120.0 if empty_err else 30.0,  # 空 body 退避更长
                    )
                    if permanent and asset_id:
                        log_db.mark_asset_unavailable(asset_id, reason=err_str[:200])

        await asyncio.gather(*[_do(j) for j in jobs])


_auto_sync_state: dict[str, Any] = {
    "running": False,           # 当前是否正在执行某一轮
    "last_run_at": 0.0,
    "last_finished_at": 0.0,
    "last_pages_scanned": 0,
    "last_queued_count": 0,
    "last_error": "",
    "next_run_at": 0.0,         # 下一轮计划开始时间戳
    "total_runs": 0,
    "total_queued_lifetime": 0,
}
_auto_sync_kick = asyncio.Event()  # 手动触发立即同步


async def _file_delete_worker() -> None:
    """后台并发 _FILE_DEL_CONCURRENCY 个云端删除任务。"""
    logger.info("file delete worker started")
    while True:
        jobs = log_db.claim_pending_deletes(_FILE_DEL_CONCURRENCY)
        if not jobs:
            _file_del_wake.clear()
            try:
                await asyncio.wait_for(_file_del_wake.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                pass
            continue

        async def _do(job: dict) -> None:
            aid = job["asset_id"]
            with account_pool.acquire() as _del_acq:
                try:
                    confirmed = await delete_grok_asset(_del_acq.settings, aid)
                    # confirmed=True：API 200 → 写 cloud_deleted_at；
                    # confirmed=False：404/410 → 不写（避免误标），让全量同步校准
                    if confirmed:
                        log_db.mark_asset_cloud_deleted(aid)
                    log_db.finish_delete(job["id"])
                except Exception as exc:
                    log_db.finish_delete(job["id"], error=str(exc))

        await asyncio.gather(*[_do(j) for j in jobs])


async def _files_auto_sync_loop() -> None:
    """常驻后台：根据 settings.files_auto_sync 周期性把未下载资产入下载队列。"""
    logger.info("files auto-sync loop started")
    while True:
        try:
            kicked = _auto_sync_kick.is_set()
            settings = settings_store.get()
            if not settings.files_auto_sync and not kicked:
                _auto_sync_state["next_run_at"] = 0.0
                # 等待手动 kick 或 60s 重新检查开关
                try:
                    await asyncio.wait_for(_auto_sync_kick.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass
                continue
            if kicked:
                _auto_sync_kick.clear()
            interval = max(60, int(settings.files_auto_sync_interval_seconds or 300))
            _auto_sync_state.update({
                "running": True, "last_run_at": time.time(),
                "last_pages_scanned": 0, "last_queued_count": 0, "last_error": "",
            })
            page_token = ""
            queued_ids: list[str] = []
            pages = 0
            try:
                with account_pool.acquire() as _auto_sync_acq:
                    for _ in range(5):
                        data = await list_grok_assets(_auto_sync_acq.settings, page_token=page_token, page_size=100)
                        assets = data.get("assets", []) or []
                        if not assets:
                            break
                        log_db.upsert_grok_assets(assets)  # auto-sync: 不消费 revived 计数
                        downloaded = log_db.get_downloaded_asset_ids()
                        unavailable = log_db.get_unavailable_asset_ids()
                        jobs = []
                        ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                                   "image/gif": ".gif", "video/mp4": ".mp4", "video/webm": ".webm"}
                        for a in assets:
                            aid = a.get("assetId") or ""
                            if not aid or aid in downloaded or aid in unavailable:
                                continue
                            mime = a.get("mimeType", "")
                            ext = ext_map.get(mime) or (("." + mime.split("/")[1]) if mime else "")
                            jobs.append({
                                "id": str(uuid.uuid4()),
                                "asset_key": a.get("key", ""),
                                "filename": aid + ext,
                                "size_bytes": int(a.get("sizeBytes") or 0),
                                "created_at": time.time(),
                            })
                            queued_ids.append(aid)
                        if jobs:
                            log_db.add_file_downloads(jobs)
                            _file_dl_wake.set()
                        pages += 1
                        _auto_sync_state["last_pages_scanned"] = pages
                        _auto_sync_state["last_queued_count"] = len(queued_ids)
                        page_token = data.get("nextPageToken", "") or ""
                        if not page_token:
                            break
            except Exception as exc:
                _auto_sync_state["last_error"] = str(exc)
                logger.warning("auto sync: list failed: %s", exc)

            _auto_sync_state.update({
                "running": False,
                "last_finished_at": time.time(),
                "next_run_at": time.time() + interval,
                "total_runs": _auto_sync_state["total_runs"] + 1,
                "total_queued_lifetime": _auto_sync_state["total_queued_lifetime"] + len(queued_ids),
            })
            if queued_ids:
                logger.info("auto sync: queued %d new files for download", len(queued_ids))
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _auto_sync_state.update({"running": False, "last_error": str(exc)})
            logger.warning("auto sync loop error: %s", exc)
            await asyncio.sleep(60)


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
    import time as _time
    from curl_cffi.requests import AsyncSession as _HBSession
    t0 = _time.monotonic()
    try:
        h = _headers(settings)
        h["Content-Type"] = "application/json"
        async with _HBSession(**_session_kwargs(settings)) as sess:
            resp = await sess.post(SKILLS_URL, headers=h, data=b'{"locale":"en"}', timeout=20.0)
            raw_sc = resp.headers.get("set-cookie", "")
            bm_refreshed = False
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
                        bm_refreshed = True
                        logger.info("heartbeat: __cf_bm refreshed")
            logger.info("heartbeat: /rest/skills OK (status=%s)", resp.status_code)
            ms = int((_time.monotonic() - t0) * 1000)
            log_db.log_system(
                event_type="heartbeat",
                status="success",
                duration_ms=ms,
                detail=f"status={resp.status_code} __cf_bm={'refreshed' if bm_refreshed else 'unchanged'}",
            )
    except Exception as exc:
        logger.warning("heartbeat failed: %s", exc)
        ms = int((_time.monotonic() - t0) * 1000)
        log_db.log_system(event_type="heartbeat", status="error", duration_ms=ms, detail=str(exc))


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
    # 单轮失败后的快速重试退避：30s, 60s, 120s, 240s（共 4 次），全失败才进入下一个主周期
    _RETRY_DELAYS = [30, 60, 120, 240]

    async def _do_refresh_once(settings) -> bool:
        """单次 FlareSolverr 刷新尝试，成功 True，失败 False。"""
        try:
            fresh_cookies, ua = await flaresolverr_refresh_cf(
                settings.flaresolverr_url,
                existing_cookies=None,
                grok_proxy=settings.grok_proxy,
                flaresolverr_proxy_url=settings.flaresolverr_proxy_url,
            )
            if not fresh_cookies:
                logger.warning("session_keeper: FlareSolverr returned empty cookies")
                log_db.log_system(event_type="cf_refresh", status="error", detail="empty cookies returned")
                return False
            cf_only_fresh = {
                k: v for k, v in fresh_cookies.items()
                if k in ("cf_clearance", "__cf_bm") and v
            }
            merged = merge_grok_cookies(settings.grok_cookie, cf_only_fresh)
            patch: dict[str, object] = {"grok_cookie": merged}
            if ua:
                patch["grok_user_agent"] = ua
                import re
                m = re.search(r"Chrome/(\d+)", ua)
                if m:
                    major = m.group(1)
                    target_browser = f"chrome{major}"
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
            log_db.log_system(
                event_type="cf_refresh", status="success",
                detail=f"cf_clearance={'yes' if 'cf_clearance' in cf_only_fresh else 'no'} "
                       f"sso={'yes' if 'sso' in merged_names else 'no'} cookies={len(merged_names)}",
            )
            return True
        except Exception as exc:
            logger.warning("session_keeper: FlareSolverr failed (%s)", exc)
            log_db.log_system(event_type="cf_refresh", status="error", detail=str(exc))
            return False

    while True:
        settings = settings_store.get()
        fs_url = settings.flaresolverr_url
        if fs_url:
            ok = await _do_refresh_once(settings)
            # 失败 → 指数退避快速重试
            if not ok:
                for i, delay in enumerate(_RETRY_DELAYS, start=1):
                    logger.info("session_keeper: cf_refresh retry %d/%d in %ds",
                                i, len(_RETRY_DELAYS), delay)
                    log_db.log_system(
                        event_type="cf_refresh_retry", status="info",
                        detail=f"attempt={i}/{len(_RETRY_DELAYS)} delay={delay}s",
                    )
                    await asyncio.sleep(delay)
                    settings = settings_store.get()  # 期间用户可能改了配置
                    if not settings.flaresolverr_url:
                        break
                    if await _do_refresh_once(settings):
                        ok = True
                        break
                if not ok:
                    logger.warning(
                        "session_keeper: all %d cf_refresh attempts failed, fallback to heartbeat",
                        len(_RETRY_DELAYS) + 1,
                    )
                    log_db.log_system(
                        event_type="cf_refresh", status="error",
                        detail=f"all {len(_RETRY_DELAYS) + 1} attempts failed; fallback to heartbeat",
                    )
                    if settings.grok_cookie:
                        await _heartbeat_once(settings)
        elif settings.grok_cookie:
            await _heartbeat_once(settings)
        interval = _KEEPER_BASE + random.randint(-_KEEPER_JITTER, _KEEPER_JITTER)
        await asyncio.sleep(interval)


# ── 配额 Poll Loop 常量 ────────────────────────────────────────────────────────
_QUOTA_POLL_INTERVAL = 300      # 5 分钟一轮（全部账号 × 模型查完后 sleep）
_QUOTA_POLL_INTER_MODEL = 2     # 同账号内模型间 sleep（秒）
_QUOTA_POLL_INTER_ACCOUNT = 5   # 跨账号 sleep（秒）
_REVALIDATE_INTERVAL = 1800     # auto_disabled 重验证间隔（30 分钟）


async def _account_quota_poll_loop() -> None:
    """每 300 秒遍历所有 enabled 账号 × 注册模型，调 query_rate_limits 更新缓存。

    错峰：每个账号之间 sleep 5s，同账号内每个模型间 sleep 2s。
    失败：捕获异常记日志后跳过，不影响其他账号/模型。
    image_model 跳过（图片配额由 query_image_rate_limits 处理，key 不同）。
    """
    while True:
        try:
            specs = [s for s in get_model_specs() if not s.image_model]
            infos = account_pool.list_accounts()
            polled = 0
            for info in infos:
                if info.status in {"manually_disabled", "auto_disabled"}:
                    continue
                if not info.enabled:
                    continue
                acc = account_pool.get_account(info.label)
                if acc is None:
                    continue
                shadow = _minimal_settings_for_poll(acc)
                for spec in specs:
                    try:
                        q = await query_rate_limits(shadow, model_name=spec.mode_id)
                        if q and "remainingQueries" in q:
                            wait = float(q.get("waitTimeSeconds") or 0)
                            reset_at = time.time() + wait
                            account_pool.update_quota(
                                info.label,
                                spec.model_id,
                                remaining=int(q["remainingQueries"]),
                                total=int(q.get("totalQueries") or 0),
                                reset_at=reset_at,
                            )
                            polled += 1
                    except Exception as exc:
                        logger.warning(
                            "quota poll failed: account=%s model=%s err=%s",
                            info.label, spec.model_id, exc,
                        )
                    await asyncio.sleep(_QUOTA_POLL_INTER_MODEL)
                await asyncio.sleep(_QUOTA_POLL_INTER_ACCOUNT)
            if polled:
                logger.info("quota poll: updated %d account-model quota entries", polled)
        except Exception:
            logger.exception("quota poll loop error")
        await asyncio.sleep(_QUOTA_POLL_INTERVAL)


async def _account_revalidate_loop() -> None:
    """每 30 分钟对 status=auto_disabled 账号做轻量探活。

    用 query_rate_limits 探一次 "auto" 模型，无异常 → re_enable + 清失败计数。
    manually_disabled 账号不动（用户主动禁用的不自动恢复）。
    """
    while True:
        await asyncio.sleep(_REVALIDATE_INTERVAL)
        try:
            infos = account_pool.list_accounts()
            for info in infos:
                if info.status != "auto_disabled":
                    continue
                acc = account_pool.get_account(info.label)
                if acc is None:
                    continue
                shadow = _minimal_settings_for_poll(acc)
                try:
                    q = await query_rate_limits(shadow, model_name="auto")
                    if q is not None:
                        account_pool.re_enable(info.label)
                        logger.info(
                            "re-validate: account=%s recovered, re-enabled", info.label
                        )
                except Exception as exc:
                    logger.debug(
                        "re-validate: account=%s still failing: %s", info.label, exc
                    )
                await asyncio.sleep(5)
        except Exception:
            logger.exception("re-validate loop error")


def _minimal_settings_for_poll(acc: "Account") -> Settings:
    """为后台 poll loop 构造该账号的 Settings（从 settings_store 派生）。"""
    from .accounts import _as_settings
    return _as_settings(settings_store.get(), acc)


def _settings() -> Settings:
    return settings_store.get()


def _require_api_key(
    request: Request,
    settings: Annotated[Settings, Depends(_settings)],
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="x-api-key")] = None,
    xgate_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    xgate_csrf_cookie: Annotated[str | None, Cookie(alias=CSRF_COOKIE)] = None,
    csrf_header: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
) -> None:
    """三通道鉴权：Bearer Header / x-api-key Header / HttpOnly Cookie。

    - 任一通道通过即可放行，前两者面向 API 客户端，cookie 面向浏览器前端。
    - cookie 通道额外要求 CSRF Double Submit：所有非 GET/HEAD/OPTIONS 请求
      必须同时携带 `xgate_csrf` cookie 与 `X-CSRF-Token` header 且两者相等。
    - api_key 留空时跳过认证（仅限受信任内网，启动时已 warning 提醒）。
    """
    if not settings.api_key:
        logger.warning("api_key 未配置，跳过认证")
        return

    # 通道 1/2：Header
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    candidates = [item for item in (bearer, x_api_key) if item]
    if any(secrets.compare_digest(settings.api_key, item) for item in candidates):
        return

    # 通道 3：HttpOnly Cookie
    sess = session_store.get(xgate_session)
    if sess is not None:
        # sliding session：touch() 在剩余 TTL < 50% 时续期，并将新 Session 存入 request.state
        touched = session_store.touch(xgate_session)
        if touched is not None and touched.expires_at != sess.expires_at:
            request.state.session_renewed = touched
        method = (request.method or "GET").upper()
        if method in {"GET", "HEAD", "OPTIONS"}:
            return
        # 状态修改请求必须做 CSRF 校验
        if (
            xgate_csrf_cookie
            and csrf_header
            and secrets.compare_digest(xgate_csrf_cookie, csrf_header)
            and secrets.compare_digest(sess.csrf, csrf_header)
        ):
            return
        raise HTTPException(
            status_code=403,
            detail=error_payload(
                "CSRF token missing or invalid",
                error_type="permission_error",
                code="csrf_failed",
            )["error"],
        )

    # 判断 401 原因：
    # - cookie 存在但 session 查不到 → 已过期或被撤销（合并为 session_revoked）
    # - cookie 不存在但有 Header candidates（都校验失败） → api_key_invalid
    # - cookie 不存在且无 Header → session_missing
    if xgate_session is not None:
        _401_code = "session_revoked"
        _401_msg = "Session expired or revoked"
    elif candidates:
        _401_code = "api_key_invalid"
        _401_msg = "Invalid API key"
    else:
        _401_code = "session_missing"
        _401_msg = "No session or API key provided"

    raise HTTPException(
        status_code=401,
        detail=error_payload(_401_msg, error_type="authentication_error", code=_401_code)["error"],
    )


def _build_tools_system_block(req: ChatCompletionRequest) -> str | None:
    """根据 req.tools / req.tool_choice 构建注入系统块文本。

    基础版 single-shot tool_call：
    - tool_choice="none"  → 返回 None（不注入）
    - tool_choice="auto" / None / "required" / {type: function, function: {name: ...}} → 注入工具描述
    - "required" 或指定 function name 时追加强制指令（best-effort，不强制约束）

    不支持（follow-up 任务）：
    - tool_choice 指定 function name 的真实强约束
    """
    if not req.tools:
        return None

    # tool_choice="none" 时完全禁用工具
    tc = req.tool_choice
    if tc == "none":
        return None

    # 构建工具描述列表
    tool_lines: list[str] = []
    for tool in req.tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") or {}
        name = fn.get("name") or tool.get("name") or "unknown"
        desc = fn.get("description") or tool.get("description") or ""
        params = fn.get("parameters") or tool.get("parameters") or {}
        params_str = json.dumps(params, ensure_ascii=False)
        tool_lines.append(
            f"TOOL: {name}\n"
            f"DESCRIPTION: {desc}\n"
            f"PARAMETERS: {params_str}"
        )

    if not tool_lines:
        return None

    block = (
        "You have access to the following tools:\n\n"
        + "\n\n".join(tool_lines)
        + "\n\n"
        "When you decide to use a tool, respond with ONLY a JSON object on a single line:\n"
        '{"tool_call": {"name": "<tool_name>", "arguments": {...}}}\n'
        "Otherwise respond normally with plain text.\n\n"
        "Previous tool calls and their results are shown in the conversation. "
        "When you have enough information, respond with plain text (not a tool_call JSON)."
    )

    # tool_choice="required" 或指定 function 时追加强制提示（best-effort）
    if tc == "required":
        block += "\n\nYou MUST call a tool to respond."
    elif isinstance(tc, dict):
        fn_name = (tc.get("function") or {}).get("name")
        if fn_name:
            block += f"\n\nYou MUST call the tool `{fn_name}` to respond."

    return block


def _extract_prompt(req: ChatCompletionRequest) -> str:
    """把 OpenAI messages 拍平成单段 prompt 文本。

    多模态块的处理：image_url / input_audio 等非文本块当前底层 Grok 通道
    不支持，转成 [image]/[audio] 占位文本，让请求继续走通而不是 400。
    未知块类型直接跳过（保兼容性，不报错）。

    工具注入（基础版 single-shot tool_call）：
    当 req.tools 非空且 tool_choice!="none" 时，在最前面拼入系统工具描述块。
    若原 messages 里已有 system，工具块拼在 system 消息之后（让用户 system 优先）。

    多轮 tool_call 支持：
    - role="assistant" 带 tool_calls（content=None）→ 序列化为 [assistant called tool ...] 块
    - role="tool" 带 tool_call_id → 反查对应 assistant tool_call 的 name，
      序列化为 [tool `<name>` returned] 块；反查失败则回退用 tool_call_id。
    """
    tools_block = _build_tools_system_block(req)

    # 预先建立 tool_call_id → name 映射，供 role="tool" 消息反查
    _call_id_to_name: dict[str, str] = {}
    for _msg in req.messages:
        if _msg.role == "assistant" and _msg.tool_calls:
            for _tc in _msg.tool_calls:
                if isinstance(_tc, dict):
                    _cid = _tc.get("id")
                    _fn = _tc.get("function") or {}
                    _name = _fn.get("name") if isinstance(_fn, dict) else None
                    if _cid and _name:
                        _call_id_to_name[_cid] = _name

    parts: list[str] = []
    tools_injected = False

    for message in req.messages:
        role = message.role
        content = message.content

        # --- role="tool"：工具执行结果 ---
        if role == "tool":
            tool_call_id = message.tool_call_id or ""
            tool_name = _call_id_to_name.get(tool_call_id, tool_call_id)
            result_text = content if isinstance(content, str) else ""
            parts.append(f"[tool `{tool_name}` returned]:\n{result_text}")
            continue

        # --- role="assistant" 带 tool_calls（content=None）：上一轮模型调工具 ---
        if role == "assistant" and message.tool_calls and content is None:
            call_lines: list[str] = []
            for tc in message.tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                tc_name = fn.get("name") if isinstance(fn, dict) else None
                tc_args = fn.get("arguments") if isinstance(fn, dict) else None
                if not tc_name:
                    continue
                # arguments 已是 JSON 字符串，直接展示
                call_lines.append(
                    f"[assistant called tool `{tc_name}` with args]:\n{tc_args or '{}'}"
                )
            if call_lines:
                parts.append("\n\n".join(call_lines))
            continue

        # --- 普通消息（str content）---
        if isinstance(content, str):
            text = content.strip()
            if text:
                parts.append(f"[{role}]: {text}")
            # 在第一条 system 消息之后注入工具块
            if role == "system" and tools_block and not tools_injected:
                parts.append(f"[system]: {tools_block}")
                tools_injected = True
            continue

        # --- 多模态列表 content ---
        if isinstance(content, list):
            segments: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text = str(block.get("text") or "").strip()
                    if text:
                        segments.append(text)
                elif block_type == "image_url":
                    segments.append("[image]")
                elif block_type in ("input_audio", "audio"):
                    segments.append("[audio]")
            joined = " ".join(segments).strip()
            if joined:
                parts.append(f"[{role}]: {joined}")

    # 若没有 system 消息，工具块放到最前面
    if tools_block and not tools_injected:
        parts.insert(0, f"[system]: {tools_block}")

    return "\n\n".join(parts).strip()


def _error_response(message: str, status: int, *, code: str | None = None) -> JSONResponse:
    # 上游 Grok 返回 401（未登录）不能原样返回，否则前端会误判 xgate 鉴权失效踢回登录
    # 401 的语义专门保留给 xgate 自身鉴权失败（_require_api_key 抛 HTTPException）
    if status == 401:
        status = 502
        code = code or "upstream_unauthorized"
    err_type = type_for_status(status)
    if code in {"cloudflare_challenge", "missing_grok_cookie", "upstream_unauthorized"}:
        err_type = "api_error"
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
# OpenAPI schema + Swagger UI / ReDoc — 鉴权后才暴露（避免匿名枚举 admin 接口）
# ---------------------------------------------------------------------------

@app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(_require_api_key)])
async def custom_openapi() -> JSONResponse:
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False, dependencies=[Depends(_require_api_key)])
async def custom_swagger_ui() -> HTMLResponse:
    return get_swagger_ui_html(openapi_url="/openapi.json", title="xGate API — Swagger")


@app.get("/redoc", include_in_schema=False, dependencies=[Depends(_require_api_key)])
async def custom_redoc() -> HTMLResponse:
    return get_redoc_html(openapi_url="/openapi.json", title="xGate API — ReDoc")


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


# ---------------------------------------------------------------------------
# Browser auth: HttpOnly cookie session
# 前端登录页用 api_key 换取 session cookie，避免把 api_key 暴露在 URL / DOM 中。
# ---------------------------------------------------------------------------


def _set_session_cookies(response: Response, request: Request, token: str, csrf: str, max_age: int = 86400) -> None:  # noqa: ARG001
    # Secure 标志由启动时一次性计算的 SECURE_COOKIE_FLAG 决定，避免反代/协议切换导致抖动。
    # SameSite=Lax：CSRF Double-Submit 已足够防御，Strict 会让外部链接跳入丢 cookie（踢出主因之一）。
    response.set_cookie(
        key=SESSION_COOKIE, value=token, httponly=True, secure=SECURE_COOKIE_FLAG,
        samesite="lax", path="/", max_age=max_age,
    )
    # CSRF cookie 需要 JS 可读，不设 HttpOnly
    response.set_cookie(
        key=CSRF_COOKIE, value=csrf, httponly=False, secure=SECURE_COOKIE_FLAG,
        samesite="lax", path="/", max_age=max_age,
    )


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


@app.post("/v1/auth/login", tags=[_TAG_ADMIN], summary="用 api_key 换取 HttpOnly session cookie",
          description=(
              "前端登录入口。请求体（form 或 JSON）携带 `api_key`，校验通过后下发：\n"
              "- `xgate_session`: HttpOnly + SameSite=Strict 的会话 cookie\n"
              "- `xgate_csrf`: 普通 cookie，前端需在状态修改请求里回填到 `X-CSRF-Token` header\n\n"
              "默认 24h 过期。"
          ))
async def auth_login(
    request: Request,
    settings: Annotated[Settings, Depends(_settings)],
    api_key: Annotated[str, Form()] = "",
) -> JSONResponse:
    submitted = (api_key or "").strip()
    if not submitted:
        # 兼容 JSON body
        try:
            body = await request.json()
            if isinstance(body, dict):
                submitted = str(body.get("api_key") or "").strip()
        except Exception:
            pass
    if not settings.api_key:
        return JSONResponse(
            error_payload("Server has no api_key configured", error_type="server_error", code="auth_disabled"),
            status_code=503,
        )
    if not submitted or not secrets.compare_digest(settings.api_key, submitted):
        return JSONResponse(
            error_payload("Invalid API key", error_type="authentication_error", code="api_key_invalid"),
            status_code=401,
        )
    sess = session_store.create()
    resp = JSONResponse({"ok": True, "csrf_token": sess.csrf, "expires_in": 86400})
    _set_session_cookies(resp, request, sess.token, sess.csrf)
    return resp


@app.post("/v1/auth/logout", tags=[_TAG_ADMIN], summary="登出，撤销 session cookie",
          description=(
              "撤销当前 cookie session 并清除浏览器 cookie。\n\n"
              "**CSRF**：若客户端持有有效 session，必须同时携带匹配的 `xgate_csrf` cookie 与 "
              "`X-CSRF-Token` header（Double Submit）。无 session 的请求幂等放行（仅清 cookie）。"
          ))
async def auth_logout(
    xgate_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    xgate_csrf_cookie: Annotated[str | None, Cookie(alias=CSRF_COOKIE)] = None,
    csrf_header: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
) -> JSONResponse:
    sess = session_store.get(xgate_session)
    if sess is not None:
        # 有有效 session 才需要 CSRF 校验：防止恶意站点 CSRF 把用户登出。
        if not (
            xgate_csrf_cookie
            and csrf_header
            and secrets.compare_digest(xgate_csrf_cookie, csrf_header)
            and secrets.compare_digest(sess.csrf, csrf_header)
        ):
            return JSONResponse(
                error_payload(
                    "CSRF token missing or invalid",
                    error_type="permission_error",
                    code="csrf_failed",
                ),
                status_code=403,
            )
    revoked = session_store.revoke(xgate_session)
    resp = JSONResponse({"ok": True, "revoked": revoked})
    _clear_session_cookies(resp)
    return resp


@app.get("/v1/auth/whoami", tags=[_TAG_ADMIN], summary="检查当前 session 是否有效",
         description="供前端启动时探测登录态：有效 cookie 返回 200 + csrf_token；无效返回 401。")
async def auth_whoami(
    request: Request,
    xgate_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> JSONResponse:
    sess = session_store.get(xgate_session)
    if sess is None:
        # cookie 存在但 session 查不到 → 过期或被撤销；cookie 不存在 → 未登录
        _code = "session_revoked" if xgate_session is not None else "session_missing"
        return JSONResponse(
            error_payload("Not logged in", error_type="authentication_error", code=_code),
            status_code=401,
        )
    # sliding session：whoami 自身也触发续期检查
    touched = session_store.touch(xgate_session)
    renewed = touched is not None and touched.expires_at != sess.expires_at
    if renewed and touched is not None:
        request.state.session_renewed = touched
        sess = touched
    return JSONResponse({
        "ok": True,
        "csrf_token": sess.csrf,
        "expires_at": int(sess.expires_at),
        "renewed": renewed,
    })


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
         summary="获取图片文件", description="直接返回图片文件内容。URL 须携带 HMAC 签名（?sig=&exp=），由图库接口自动签发。")
async def serve_image(
    request: Request,
    session_id: str,
    filename: str,
    sig: str = "",
    exp: int = 0,
) -> FileResponse:
    url_path = request.url.path
    api_key = settings_store.get().api_key
    if not sig:
        if _SIGNED_URL_ENFORCED:
            raise HTTPException(status_code=403, detail="Signature required")
        logger.warning("unsigned file access: %s", url_path)
    elif not verify_signed_path(url_path, sig, exp, api_key):
        raise HTTPException(status_code=403, detail="Invalid or expired signature")
    safe_sid = Path(session_id).name
    safe_fn = Path(filename).name
    path = IMAGE_DIR / safe_sid / safe_fn
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(str(path))


_VIDEO_MEDIA_TYPES = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}


@app.get("/v1/files/video/{session_id}/{filename}", tags=[_TAG_GALLERY],
         summary="获取视频文件", description="直接返回视频文件内容。支持 mp4 / webm / mov，返回正确的 Content-Type。URL 须携带 HMAC 签名（?sig=&exp=）。")
async def serve_video(
    request: Request,
    session_id: str,
    filename: str,
    sig: str = "",
    exp: int = 0,
) -> FileResponse:
    url_path = request.url.path
    api_key = settings_store.get().api_key
    if not sig:
        if _SIGNED_URL_ENFORCED:
            raise HTTPException(status_code=403, detail="Signature required")
        logger.warning("unsigned file access: %s", url_path)
    elif not verify_signed_path(url_path, sig, exp, api_key):
        raise HTTPException(status_code=403, detail="Invalid or expired signature")
    safe_sid = Path(session_id).name
    safe_fn = Path(filename).name
    path = IMAGE_DIR / safe_sid / safe_fn
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Video not found")
    media_type = _VIDEO_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(str(path), media_type=media_type)


@app.get(
    "/v1/files/proxy",
    tags=[_TAG_FILES],
    summary="代理拉取 assets.grok.com 资源（MCP 图片 URL 用）",
    description=(
        "MCP `grok_imagine` 工具生成图片后返回此 URL，由 xGate 携带 Cookie 转发拉取，"
        "MCP 客户端无需持有 Cookie。仅允许代理 `assets.grok.com` 域名。"
    ),
    dependencies=[Depends(_require_api_key)],
)
async def proxy_grok_asset(
    request: Request,
    settings: Annotated[Settings, Depends(_settings)],
    url: str = "",
    xgate_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> StreamingResponse:
    from urllib.parse import urlparse, unquote

    # CSRF 防护：cookie 通道必须有 same-origin Referer 或 Origin
    # API 客户端（Bearer / X-Api-Key）无 session cookie，直接放行
    if xgate_session is not None:
        referer = request.headers.get("referer", "")
        origin = request.headers.get("origin", "")
        # 允许的 host 集合：本服务实际 host + public_base_url 配置的 host
        allowed_hosts: set[str] = set()
        if request.url.hostname:
            allowed_hosts.add(request.url.hostname)
        pub = (settings.public_base_url or "").strip()
        if pub:
            try:
                _pub_host = urlparse(pub).hostname
                if _pub_host:
                    allowed_hosts.add(_pub_host)
            except Exception:
                pass

        def _host_allowed(header_val: str) -> bool:
            if not header_val:
                return False
            try:
                return (urlparse(header_val).hostname or "") in allowed_hosts
            except Exception:
                return False

        if not (_host_allowed(referer) or _host_allowed(origin)):
            return JSONResponse(
                {
                    "error": {
                        "message": (
                            "This endpoint is GET-only by design (for <img src>) and requires "
                            "same-origin Referer when called via session cookie."
                        ),
                        "type": "permission_error",
                        "code": "cross_origin_blocked",
                    }
                },
                status_code=403,
            )

    if not url:
        return JSONResponse({"error": "url is required"}, status_code=400)
    try:
        parsed = urlparse(unquote(url))
    except Exception:
        return JSONResponse({"error": "invalid url"}, status_code=400)
    if parsed.netloc != "assets.grok.com":
        return JSONResponse({"error": "only assets.grok.com is allowed"}, status_code=403)
    key = parsed.path.lstrip("/")
    if not key:
        return JSONResponse({"error": "empty asset path"}, status_code=400)
    with account_pool.acquire() as acq:
        try:
            content_type, gen = await stream_grok_asset(acq.settings, key)
            return StreamingResponse(gen, media_type=content_type,
                                     headers={"Cache-Control": "private, max-age=43200"})
        except GrokClientError as exc:
            acq.mark_failure(exc.code or "upstream_5xx", retry_after=exc.retry_after)
            return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
        except Exception:
            acq.mark_failure("upstream_5xx")
            logger.exception("proxy_grok_asset failed")
            return JSONResponse({"error": "Internal server error"}, status_code=500)


@app.get("/v1/grok-files/", tags=[_TAG_FILES], summary="列出本地已下载 Grok 文件（DB 优先，filesystem 兜底）",
         dependencies=[Depends(_require_api_key)])
async def list_local_grok_files() -> JSONResponse:
    """返回已下载 asset_id 列表（去扩展名）。"""
    from .grok_client import GROK_FILES_DIR
    db_ids = log_db.get_downloaded_asset_ids()
    if db_ids:
        return JSONResponse({"files": list(db_ids)})
    # 兜底：DB 为空时扫目录
    GROK_FILES_DIR.mkdir(parents=True, exist_ok=True)
    ids = [
        f.stem for f in GROK_FILES_DIR.iterdir()
        if f.is_file() and not f.name.startswith(".")
    ]
    return JSONResponse({"files": ids})


@app.get("/v1/grok-files/assets", tags=[_TAG_FILES], summary="从本地 DB 分页查询资产元数据",
         dependencies=[Depends(_require_api_key)])
async def list_db_assets(
    page: int = 0,
    page_size: int = 50,
    only_undownloaded: bool = False,
) -> JSONResponse:
    page_size = min(max(page_size, 1), 200)
    offset = max(page, 0) * page_size
    assets = log_db.list_grok_assets_db(
        only_undownloaded=only_undownloaded, limit=page_size, offset=offset,
    )
    total = log_db.count_grok_assets(only_undownloaded=only_undownloaded)
    return JSONResponse({
        "assets": assets,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": offset + len(assets) < total,
    })


_sync_lock = asyncio.Lock()
_sync_state: dict[str, Any] = {"running": False, "progress": 0, "total_pages": 0,
                               "new_count": 0, "updated_count": 0,
                               "revived_count": 0,            # 误标 cloud_deleted 但实际仍在云端 → 已纠正的数量
                               "stale_deleted_count": 0,      # 同步结束后仍标 cloud_deleted 但实际仍在云端 → 已纠正的数量
                               "scanned_asset_ids": 0,        # 本轮扫到的 asset_id 数（用于交叉校验）
                               "started_at": 0.0,
                               "finished_at": 0.0, "error": ""}


@app.get("/v1/grok-files/sync/status", tags=[_TAG_FILES], summary="查询全量同步状态",
         dependencies=[Depends(_require_api_key)])
async def grok_sync_status() -> JSONResponse:
    return JSONResponse(_sync_state)


class _AutoSyncToggle(BaseModel):
    enabled: bool
    interval_seconds: int = 300


@app.get("/v1/grok-files/auto-sync", tags=[_TAG_FILES], summary="查询自动同步开关 + 运行状态",
         dependencies=[Depends(_require_api_key)])
async def get_auto_sync_status() -> JSONResponse:
    s = settings_store.get()
    # 下载队列概况（最近 1h 各状态计数）
    overview = log_db.get_dl_queue_overview(since_ts=time.time() - 3600)
    counts = {
        "pending":  overview.get("pending", 0),
        "running":  overview.get("running", 0),
        "retrying": overview.get("retrying", 0),
        "done":     overview.get("done", 0),
        "failed":   overview.get("failed", 0),
    }
    return JSONResponse({
        "enabled": s.files_auto_sync,
        "interval_seconds": s.files_auto_sync_interval_seconds,
        "state": _auto_sync_state,
        "download_queue_recent": counts,
    })


@app.post("/v1/grok-files/auto-sync", tags=[_TAG_FILES], summary="设置自动同步开关（持久化到配置）",
          dependencies=[Depends(_require_api_key)])
async def set_auto_sync(req: _AutoSyncToggle) -> JSONResponse:
    interval = max(60, int(req.interval_seconds or 300))
    new = settings_store.update(files_auto_sync=bool(req.enabled), files_auto_sync_interval_seconds=interval)
    return JSONResponse({"enabled": new.files_auto_sync, "interval_seconds": new.files_auto_sync_interval_seconds})


@app.post("/v1/grok-files/sync-now", tags=[_TAG_FILES],
          summary="立即触发：把 DB 中所有未下载资产批量入下载队列",
          dependencies=[Depends(_require_api_key)])
async def grok_sync_now() -> JSONResponse:
    """从本地 DB 读取所有未下载且未在云端删除的资产，批量入队。"""
    rows = log_db.list_grok_assets_db(only_undownloaded=True, limit=100000, offset=0)
    if not rows:
        # 唤醒后台 worker 顺带刷一次（DB 可能还没 populated）
        _auto_sync_kick.set()
        return JSONResponse({"queued": 0, "message": "DB 中暂无未下载资产；已唤醒后台扫描"})
    ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
               "image/gif": ".gif", "video/mp4": ".mp4", "video/webm": ".webm"}
    jobs = []
    for a in rows:
        # 跳过已被云端删除的（虽然 DB 还在）
        if a.get("cloud_deleted_at"):
            continue
        aid = a.get("assetId") or ""
        if not aid:
            continue
        mime = a.get("mimeType", "")
        ext = ext_map.get(mime) or (("." + mime.split("/")[1]) if mime else "")
        jobs.append({
            "id": str(uuid.uuid4()),
            "asset_key": a.get("key", ""),
            "filename": aid + ext,
            "size_bytes": int(a.get("sizeBytes") or 0),
            "created_at": time.time(),
        })
    if jobs:
        log_db.add_file_downloads(jobs)
        _file_dl_wake.set()
    # 同时 kick 一次 sync loop，让它检查云端是否有 DB 没缓存的新资产
    _auto_sync_kick.set()
    return JSONResponse({"queued": len(jobs), "scanned_assets": len(rows)})


@app.post("/v1/grok-files/sync", tags=[_TAG_FILES], summary="启动全量同步：拉取所有 Grok Files API 页 → 入库",
          dependencies=[Depends(_require_api_key)])
async def grok_full_sync(settings: Annotated[Settings, Depends(_settings)]) -> JSONResponse:
    if _sync_state["running"]:
        raise HTTPException(409, "Sync already running")

    async def _run() -> None:
        async with _sync_lock:
            _sync_state.update({
                "running": True, "progress": 0, "total_pages": 0,
                "new_count": 0, "updated_count": 0,
                "revived_count": 0, "stale_deleted_count": 0, "scanned_asset_ids": 0,
                "started_at": time.time(), "finished_at": 0.0, "error": "",
            })
            seen_ids: set[str] = set()
            try:
                with account_pool.acquire() as _sync_acq:
                    page_token = ""
                    pages = 0
                    MAX_PAGES = 200  # 安全上限：~10000 个 asset
                    while pages < MAX_PAGES:
                        data = await list_grok_assets(_sync_acq.settings, page_token=page_token, page_size=100)
                        assets = data.get("assets", []) or []
                        if not assets:
                            break
                        new_n, upd_n, rev_n = log_db.upsert_grok_assets(assets)
                        _sync_state["new_count"] += new_n
                        _sync_state["updated_count"] += upd_n
                        _sync_state["revived_count"] += rev_n
                        for a in assets:
                            aid = a.get("assetId") or ""
                            if aid:
                                seen_ids.add(aid)
                        pages += 1
                        _sync_state["progress"] = pages
                        page_token = data.get("nextPageToken", "") or ""
                        if not page_token:
                            break
                    _sync_state["total_pages"] = pages
                    _sync_state["scanned_asset_ids"] = len(seen_ids)
                    # 跑完一遍全量后，DB 里仍标 cloud_deleted_at 的若出现在本轮 listing → 之前误标，统一清空
                    # 注意：upsert_grok_assets 已经在每页清掉 cloud_deleted_at；这里做兜底校验防止任何漏网
                    if seen_ids:
                        try:
                            ids_list = list(seen_ids)
                            BATCH = 500  # SQLite 变量上限保护
                            stale_total = 0
                            with log_db._connect() as conn:
                                for i in range(0, len(ids_list), BATCH):
                                    chunk = ids_list[i:i + BATCH]
                                    ph = ",".join("?" * len(chunk))
                                    cur = conn.execute(
                                        f"SELECT COUNT(*) FROM grok_assets"
                                        f" WHERE cloud_deleted_at IS NOT NULL AND asset_id IN ({ph})",
                                        chunk,
                                    )
                                    n = cur.fetchone()[0] or 0
                                    if n:
                                        conn.execute(
                                            f"UPDATE grok_assets SET cloud_deleted_at=NULL"
                                            f" WHERE cloud_deleted_at IS NOT NULL AND asset_id IN ({ph})",
                                            chunk,
                                        )
                                    stale_total += n
                                if stale_total:
                                    logger.info("full sync: cleared cloud_deleted_at on %d stale records", stale_total)
                                _sync_state["stale_deleted_count"] = stale_total
                        except Exception as exc:
                            logger.warning("full sync stale-deleted reconcile failed: %s", exc)
            except Exception as exc:
                _sync_state["error"] = str(exc)
                logger.exception("grok full sync failed")
            finally:
                _sync_state["running"] = False
                _sync_state["finished_at"] = time.time()

    asyncio.create_task(_run())
    return JSONResponse({"started": True})


class _DlQueueItem(BaseModel):
    asset_key: str
    filename: str
    size_bytes: int = 0


# 注意：以下两个 /downloads 路由必须在 /{filename} 之前注册，否则会被参数路由拦截
class _DeleteBatch(BaseModel):
    asset_ids: list[str]


@app.post("/v1/grok-files/deletes", tags=[_TAG_FILES],
          summary="提交批量云端删除到后台队列（并发上限 2）",
          dependencies=[Depends(_require_api_key)])
async def queue_file_deletes(req: _DeleteBatch) -> JSONResponse:
    n = log_db.add_file_deletes(req.asset_ids or [])
    if n > 0:
        _file_del_wake.set()
    return JSONResponse({"queued": n})


@app.post("/v1/grok-files/wipe-cloud", tags=[_TAG_FILES],
          summary="⚠️ 清空所有云端文件（不删本地）",
          description=(
              "把 DB 中所有 cloud_deleted_at IS NULL 的 asset_id 全部入删除队列。\n"
              "**只删云端，本地已下载文件保留**。两次拟态框确认后由前端调用。"
          ),
          dependencies=[Depends(_require_api_key)])
async def wipe_cloud() -> JSONResponse:
    try:
        with log_db._connect() as conn:
            rows = conn.execute(
                "SELECT asset_id FROM grok_assets"
                " WHERE cloud_deleted_at IS NULL AND asset_id IS NOT NULL AND asset_id != ''"
            ).fetchall()
            ids = [r[0] for r in rows]
    except Exception as exc:
        return _error_response(f"DB query failed: {exc}", 500)
    if not ids:
        return JSONResponse({"queued": 0, "message": "DB 中无可删除的云端 asset"})
    n = log_db.add_file_deletes(ids)
    if n > 0:
        _file_del_wake.set()
    return JSONResponse({"queued": n, "total_assets": len(ids)})


@app.get("/v1/grok-files/deletes", tags=[_TAG_FILES], summary="查询删除队列",
         description=(
             "查询 file_deletes 状态。两种用法：\n"
             "- `?since=<ts>`：返回该时间戳之后的任务（LIMIT 500，按 created_at 倒序）\n"
             "- `?asset_ids=a,b,c`（逗号分隔）：精确按 asset_id 查每个的最新一条状态\n"
             "  传 asset_ids 时不受 LIMIT 影响，前端轮询用这种方式避免一次大批入队后被截断。"
         ),
         dependencies=[Depends(_require_api_key)])
async def list_file_deletes(since: float = 0.0, asset_ids: str = "") -> JSONResponse:
    try:
        with log_db._connect() as conn:
            if asset_ids:
                ids = [s.strip() for s in asset_ids.split(",") if s.strip()]
                if not ids:
                    return JSONResponse({"deletes": []})
                # 每个 asset_id 取 created_at 最新一行（多次入队场景下也准确）
                BATCH = 400
                out: list[dict] = []
                for i in range(0, len(ids), BATCH):
                    chunk = ids[i:i + BATCH]
                    ph = ",".join("?" * len(chunk))
                    rows = conn.execute(
                        f"SELECT t.* FROM file_deletes t"
                        f" JOIN (SELECT asset_id, MAX(created_at) mx FROM file_deletes"
                        f"        WHERE asset_id IN ({ph}) GROUP BY asset_id) g"
                        f"   ON t.asset_id=g.asset_id AND t.created_at=g.mx",
                        chunk,
                    ).fetchall()
                    out.extend(dict(r) for r in rows)
                return JSONResponse({"deletes": out})
            rows = conn.execute(
                "SELECT * FROM file_deletes WHERE created_at>=?"
                " ORDER BY created_at DESC LIMIT 500", (since,)
            ).fetchall()
            return JSONResponse({"deletes": [dict(r) for r in rows]})
    except Exception:
        return JSONResponse({"deletes": []})


@app.post("/v1/grok-files/downloads", tags=[_TAG_FILES], summary="提交批量下载到后台队列",
          dependencies=[Depends(_require_api_key)])
async def queue_file_downloads(items: list[_DlQueueItem]) -> JSONResponse:
    if not items:
        raise HTTPException(400, "No items provided")
    jobs = [
        {
            "id": str(uuid.uuid4()),
            "asset_key": it.asset_key,
            "filename": it.filename,
            "size_bytes": it.size_bytes,
            "created_at": time.time(),
        }
        for it in items
    ]
    log_db.add_file_downloads(jobs)
    _file_dl_wake.set()
    return JSONResponse({"queued": len(jobs), "ids": [j["id"] for j in jobs]})


@app.get("/v1/grok-files/downloads", tags=[_TAG_FILES], summary="查询下载队列状态",
         dependencies=[Depends(_require_api_key)])
async def list_file_downloads(since: float = 0.0) -> JSONResponse:
    jobs = log_db.list_file_downloads(since_ts=since)
    return JSONResponse({"downloads": jobs})


@app.delete("/v1/grok-files/{filename}", tags=[_TAG_FILES],
            summary="删除本地已下载的 Grok 文件",
            dependencies=[Depends(_require_api_key)])
async def delete_local_grok_file(filename: str) -> JSONResponse:
    from .grok_client import GROK_FILES_DIR
    safe_fn = Path(filename).name
    path = GROK_FILES_DIR / safe_fn
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        path.unlink()
        # 同步清掉 DB 里的 downloaded 标记
        asset_id = safe_fn.rsplit(".", 1)[0] if "." in safe_fn else safe_fn
        try:
            with log_db._connect() as conn:
                conn.execute(
                    "UPDATE grok_assets SET local_path='', downloaded_at=NULL WHERE asset_id=?",
                    (asset_id,),
                )
        except Exception:
            pass
    except Exception as exc:
        logger.exception("local grok file delete failed: %s", safe_fn)
        raise HTTPException(status_code=500, detail="Delete operation failed") from exc
    return JSONResponse({"ok": True, "filename": safe_fn})


@app.get("/v1/grok-files/{filename}", tags=[_TAG_FILES],
         summary="读取本地 Grok 文件（HMAC 签名 URL）")
async def serve_grok_file(
    request: Request,
    filename: str,
    sig: str = "",
    exp: int = 0,
) -> FileResponse:
    from .grok_client import GROK_FILES_DIR
    url_path = request.url.path
    api_key = settings_store.get().api_key
    if not sig:
        if _SIGNED_URL_ENFORCED:
            raise HTTPException(status_code=403, detail="Signature required")
        logger.warning("unsigned file access: %s", url_path)
    elif not verify_signed_path(url_path, sig, exp, api_key):
        raise HTTPException(status_code=403, detail="Invalid or expired signature")
    safe_fn = Path(filename).name
    path = GROK_FILES_DIR / safe_fn
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(path))


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
    rid = response_id()
    t0 = time.monotonic()
    prompt_text = req.prompt.strip()
    aspect_ratio = req.aspect_ratio or "16:9"
    resolution = req.resolution or "480p"
    logger.info("video: prompt=%r duration=%ss resolution=%s aspect=%s",
                prompt_text[:80], duration_sec, resolution, aspect_ratio)
    import uuid as _uuid
    session_id = str(_uuid.uuid4())
    video_id: str | None = None
    _acq_label = ""
    with account_pool.acquire(model_id="grok-imagine-video") as acq:
        _acq_label = acq.label
        monitor.record_start(acq.label)
        try:
            video_id = await create_video(
                acq.settings,
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
            acq.mark_failure(exc.code or "upstream_5xx", retry_after=exc.retry_after)
            monitor.record_failure(acq.label, exc.status_code, str(exc), cloudflare=exc.code == "cloudflare_challenge")
            log_db.log_video(
                request_id=rid, model="grok-imagine-video", prompt=prompt_text,
                session_id=session_id, aspect_ratio=aspect_ratio,
                duration_sec=duration_sec, resolution=resolution, source="api",
                status="error", duration_ms=int((time.monotonic() - t0) * 1000), error=str(exc),
                account_label=acq.label,
            )
            return _error_response(msg, exc.status_code, code=exc.code or "upstream_error")
        except Exception as exc:
            acq.mark_failure("upstream_5xx")
            logger.exception("video generation failed")
            monitor.record_failure(acq.label, 500, str(exc))
            log_db.log_video(
                request_id=rid, model="grok-imagine-video", prompt=prompt_text,
                session_id=session_id, aspect_ratio=aspect_ratio,
                duration_sec=duration_sec, resolution=resolution, source="api",
                status="error", duration_ms=int((time.monotonic() - t0) * 1000), error=str(exc),
                account_label=acq.label,
            )
            return _error_response("Internal server error", 500)
    monitor.record_success(_acq_label)
    log_db.log_video(
        request_id=rid, model="grok-imagine-video", prompt=prompt_text,
        video_path=f"data/images/{session_id}/{video_id}.mp4",
        session_id=session_id, aspect_ratio=aspect_ratio,
        duration_sec=duration_sec, resolution=resolution, source="api",
        status="success", duration_ms=int((time.monotonic() - t0) * 1000),
        account_label=_acq_label,
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
    with account_pool.acquire(model_id="grok-imagine-video") as acq:
        try:
            media_url = await get_video_link(acq.settings, video_id)
        except GrokClientError as exc:
            acq.mark_failure(exc.code or "upstream_5xx", retry_after=exc.retry_after)
            return _error_response(str(exc), exc.status_code, code=exc.code or "upstream_error")
        except Exception:
            acq.mark_failure("upstream_5xx")
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
    model_id = req.model or settings.default_image_model
    spec = get_model(model_id)
    if spec is None or not spec.image_model:
        return _error_response(f"Model {model_id!r} not found or not an image model", 404, code="model_not_found")
    if not req.prompt.strip():
        return _error_response("prompt cannot be empty", 400, code="invalid_prompt")
    if req.response_format and req.response_format != "url":
        return _error_response(
            f"response_format={req.response_format!r} is not supported, only 'url' is available",
            400,
            code="unsupported_parameter",
        )

    prompt = req.prompt.strip()
    aspect_ratio = resolve_aspect_ratio(req.size)
    logger.info("image: model=%s prompt=%r aspect=%s n=%d",
                model_id, prompt[:80], aspect_ratio, req.n)
    session_id = str(__import__("uuid").uuid4())
    session_dir = _init_session(session_id, prompt=prompt, source="api", aspect_ratio=aspect_ratio)
    t0 = time.monotonic()
    monitor.record_start()
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
        monitor.record_failure(status=exc.status_code, summary=str(exc), cloudflare=code == "cloudflare_challenge")
        log_db.log_image(
            request_id=session_id, model=model_id, prompt=prompt,
            aspect_ratio=aspect_ratio, source="api",
            status="error", duration_ms=int((time.monotonic() - t0) * 1000), error=str(exc),
        )
        return _error_response(str(exc), exc.status_code, code=code)
    except Exception as exc:
        logger.exception("image generation failed")
        monitor.record_failure(status=500, summary=str(exc))
        log_db.log_image(
            request_id=session_id, model=model_id, prompt=prompt,
            aspect_ratio=aspect_ratio, source="api",
            status="error", duration_ms=int((time.monotonic() - t0) * 1000), error=str(exc),
        )
        return _error_response("Internal server error", 500)

    monitor.record_success()
    log_db.log_image(
        request_id=session_id, model=model_id, prompt=prompt,
        image_paths=[img.serve_path for img in images],
        image_count=len(images),
        aspect_ratio=aspect_ratio, source="api",
        status="success", duration_ms=int((time.monotonic() - t0) * 1000),
    )
    base = _base_url(settings)

    def _item(img: ImageResult) -> dict:
        raw_path = f"/v1/files/image/{img.serve_path}"
        url = base + _sign_file_url(raw_path)
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
        "**可用模型**：见 `GET /v1/models`，默认对话模型为 `grok-4.20-auto`。\n\n"
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
    logger.info("chat: model=%s stream=%s prompt_len=%d", req.model, req.stream, len(prompt))
    include_usage = bool(req.stream_options and req.stream_options.include_usage)
    max_out = req.max_completion_tokens or req.max_tokens

    # 判断是否有有效工具请求（tool_choice!="none" 且 tools 非空）
    has_tools = bool(req.tools and req.tool_choice != "none")

    if req.stream:
        rid = response_id()
        t0 = time.monotonic()

        # 流式 tool_call 简化方案：有工具请求时强制走非流式后台执行，
        # 结果以单个 SSE chunk 发出。
        # 流式 tool_call 增量（delta arguments）是 follow-up 任务，暂不实现。
        if has_tools:
            async def generate_tools_as_stream() -> AsyncGenerator[str, None]:
                with account_pool.acquire(model_id=req.model) as acq:
                    try:
                        content = await complete_chat(acq.settings, message=prompt, mode_id=spec.mode_id)
                        tc = parse_tool_call(content)
                        if tc is not None:
                            # tool_call 分支：整段以单 chunk 发出（finish_reason=tool_calls）
                            tool_name = tc["name"]
                            tool_args = tc.get("arguments", {})
                            args_str = json.dumps(tool_args, ensure_ascii=False) if not isinstance(tool_args, str) else tool_args
                            delta: dict[str, Any] = {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": f"call_{secrets.token_hex(12)}",
                                        "type": "function",
                                        "function": {"name": tool_name, "arguments": args_str},
                                    }
                                ],
                            }
                            chunk: dict[str, Any] = {
                                "id": rid,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": req.model,
                                "system_fingerprint": "fp_xgate",
                                "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": "tool_calls"}],
                            }
                            yield sse_data(chunk)
                            if include_usage:
                                yield sse_data(stream_usage_chunk(rid, req.model, prompt, content))
                            yield "data: [DONE]\n\n"
                            monitor.record_success(acq.label)
                            log_db.log_chat(
                                request_id=rid, model=req.model, prompt=prompt,
                                response=content,
                                status="success", duration_ms=int((time.monotonic() - t0) * 1000),
                                account_label=acq.label,
                            )
                        else:
                            # 普通 content 分支，走标准流式输出
                            finish_reason = "stop"
                            if max_out is not None and max(1, len(content) // 4) >= max_out:
                                finish_reason = "length"
                                content = content[: max_out * 4]
                            yield sse_data(stream_chunk(rid, req.model, content, role="assistant"))
                            yield sse_data(stream_chunk(rid, req.model, "", finish_reason=finish_reason, role=None))
                            if include_usage:
                                yield sse_data(stream_usage_chunk(rid, req.model, prompt, content))
                            yield "data: [DONE]\n\n"
                            monitor.record_success(acq.label)
                            log_db.log_chat(
                                request_id=rid, model=req.model, prompt=prompt,
                                response=content,
                                status="success", duration_ms=int((time.monotonic() - t0) * 1000),
                                account_label=acq.label,
                            )
                    except GrokClientError as exc:
                        code = exc.code or "upstream_error"
                        acq.mark_failure(code, retry_after=exc.retry_after)
                        monitor.record_failure(acq.label, exc.status_code, str(exc), cloudflare=code == "cloudflare_challenge")
                        log_db.log_chat(
                            request_id=rid, model=req.model, prompt=prompt,
                            status="error", duration_ms=int((time.monotonic() - t0) * 1000), error=str(exc),
                            account_label=acq.label,
                        )
                        yield sse_error(str(exc), error_type="api_error", code=code)
                        yield "data: [DONE]\n\n"
                    except Exception as exc:
                        acq.mark_failure("upstream_5xx")
                        monitor.record_failure(acq.label, 500, str(exc))
                        log_db.log_chat(
                            request_id=rid, model=req.model, prompt=prompt,
                            status="error", duration_ms=int((time.monotonic() - t0) * 1000), error=str(exc),
                            account_label=acq.label,
                        )
                        yield sse_error(str(exc))
                        yield "data: [DONE]\n\n"

            return StreamingResponse(
                generate_tools_as_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )

        async def generate() -> AsyncGenerator[str, None]:
            chunks: list[str] = []
            finish_reason = "stop"
            first_chunk = True
            # max_out 是目标 token 数；用 4 字符≈1 token 的粗估转成字符上限
            char_budget = max_out * 4 if max_out is not None else None
            with account_pool.acquire(model_id=req.model) as acq:
                try:
                    async for delta in stream_chat(acq.settings, message=prompt, mode_id=spec.mode_id):
                        if delta.done:
                            break
                        piece = delta.content
                        if char_budget is not None:
                            emitted = sum(len(c) for c in chunks)
                            remaining = char_budget - emitted
                            if remaining <= 0:
                                finish_reason = "length"
                                break
                            if len(piece) > remaining:
                                piece = piece[:remaining]
                                chunks.append(piece)
                                yield sse_data(stream_chunk(
                                    rid, req.model, piece,
                                    role="assistant" if first_chunk else None,
                                ))
                                finish_reason = "length"
                                break
                        chunks.append(piece)
                        yield sse_data(stream_chunk(
                            rid, req.model, piece,
                            role="assistant" if first_chunk else None,
                        ))
                        first_chunk = False
                    yield sse_data(stream_chunk(rid, req.model, "", finish_reason=finish_reason, role=None))
                    if include_usage:
                        yield sse_data(stream_usage_chunk(rid, req.model, prompt, "".join(chunks)))
                    yield "data: [DONE]\n\n"
                    monitor.record_success(acq.label)
                    logger.info("chat: model=%s done stream=True in %.1fs chunks=%d",
                                req.model, time.monotonic() - t0, len(chunks))
                    log_db.log_chat(
                        request_id=rid, model=req.model, prompt=prompt,
                        response="".join(chunks),
                        status="success", duration_ms=int((time.monotonic() - t0) * 1000),
                        account_label=acq.label,
                    )
                except GrokClientError as exc:
                    code = exc.code or "upstream_error"
                    acq.mark_failure(code, retry_after=exc.retry_after)
                    monitor.record_failure(acq.label, exc.status_code, str(exc), cloudflare=code == "cloudflare_challenge")
                    log_db.log_chat(
                        request_id=rid, model=req.model, prompt=prompt,
                        status="error", duration_ms=int((time.monotonic() - t0) * 1000), error=str(exc),
                        account_label=acq.label,
                    )
                    yield sse_error(str(exc), error_type="api_error", code=code)
                    yield "data: [DONE]\n\n"
                except Exception as exc:
                    acq.mark_failure("upstream_5xx")
                    monitor.record_failure(acq.label, 500, str(exc))
                    log_db.log_chat(
                        request_id=rid, model=req.model, prompt=prompt,
                        status="error", duration_ms=int((time.monotonic() - t0) * 1000), error=str(exc),
                        account_label=acq.label,
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
    _chat_acq_label = ""
    with account_pool.acquire(model_id=req.model) as acq:
        _chat_acq_label = acq.label
        try:
            content = await complete_chat(acq.settings, message=prompt, mode_id=spec.mode_id)
        except GrokClientError as exc:
            code = exc.code or "upstream_error"
            acq.mark_failure(code, retry_after=exc.retry_after)
            monitor.record_failure(acq.label, exc.status_code, str(exc), cloudflare=code == "cloudflare_challenge")
            log_db.log_chat(
                request_id=rid, model=req.model, prompt=prompt,
                status="error", duration_ms=int((time.monotonic() - t0) * 1000), error=str(exc),
                account_label=acq.label,
            )
            return _error_response(str(exc), exc.status_code, code=code)
        except Exception as exc:
            acq.mark_failure("upstream_5xx")
            logger.exception("chat completion failed")
            monitor.record_failure(acq.label, 500, str(exc))
            log_db.log_chat(
                request_id=rid, model=req.model, prompt=prompt,
                status="error", duration_ms=int((time.monotonic() - t0) * 1000), error=str(exc),
                account_label=acq.label,
            )
            return _error_response("Internal server error", 500)

    logger.info("chat: model=%s done stream=False in %.1fs len=%d",
                req.model, time.monotonic() - t0, len(content))

    # 工具调用解析（基础版 single-shot tool_call）：
    # 若有工具请求，尝试解析模型输出是否是 tool_call JSON 格式。
    if has_tools:
        tc = parse_tool_call(content)
        if tc is not None:
            tool_name = tc["name"]
            tool_args = tc.get("arguments", {})
            monitor.record_success(_chat_acq_label)
            log_db.log_chat(
                request_id=rid, model=req.model, prompt=prompt, response=content,
                status="success", duration_ms=int((time.monotonic() - t0) * 1000),
                account_label=_chat_acq_label,
            )
            return JSONResponse(chat_response_tool_call(req.model, tool_name, tool_args, prompt, rid=rid))

    finish_reason = "stop"
    if max_out is not None and count_tokens(content) >= max_out:
        finish_reason = "length"
        # 按字符近似截断到目标 token 上限
        content = content[: max_out * 4]
    monitor.record_success(_chat_acq_label)
    log_db.log_chat(
        request_id=rid, model=req.model, prompt=prompt, response=content,
        status="success", duration_ms=int((time.monotonic() - t0) * 1000),
        account_label=_chat_acq_label,
    )
    return JSONResponse(chat_response(req.model, content, prompt, rid=rid, finish_reason=finish_reason))


# ---------------------------------------------------------------------------
# OpenAI 兼容占位：未实现的 endpoint 返回 501 + not_implemented，
# 比 404 对客户端更友好（说明语义已知但本服务不支持）。
# ---------------------------------------------------------------------------

def _not_implemented(endpoint: str) -> JSONResponse:
    return JSONResponse(
        error_payload(
            f"{endpoint} is not implemented by xGate",
            error_type="invalid_request_error",
            code="not_implemented",
        ),
        status_code=501,
    )


# Unimplemented OpenAI stubs intentionally omitted — clients that depend on
# /v1/embeddings, /v1/completions, /v1/moderations, /v1/audio/* will get 404.


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
async def stream_start(
    req: ImageStreamStartRequest,
    settings: Annotated[Settings, Depends(_settings)],
) -> JSONResponse:
    model_id = req.model or settings.default_image_model
    spec = get_model(model_id)
    if spec is None or not spec.image_model:
        return _error_response(f"Model {model_id!r} not found or not an image model", 404, code="model_not_found")
    if not req.prompt.strip():
        return _error_response("prompt cannot be empty", 400, code="invalid_prompt")
    if image_stream_worker.is_running():
        return JSONResponse({"ok": False, "message": "worker already running"}, status_code=409)
    cfg = StreamConfig(
        prompt=req.prompt.strip(),
        model=model_id,
        n=req.n,
        size=req.size,
        interval_seconds=req.interval_seconds,
        max_rounds=req.max_rounds,
        max_images=req.max_images,
        enable_pro=spec.enable_pro,
        image_data=req.image_data or None,
    )
    session_id = image_stream_worker.start(ws_gateway, cfg, log_db=log_db, task_queue=task_queue)
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
    model_id = req.model or settings.default_image_model
    spec = get_model(model_id)
    if spec is None or not spec.image_model:
        return _error_response(f"Model {model_id!r} not found or not an image model", 404, code="model_not_found")
    if not req.prompt.strip():
        return _error_response("prompt cannot be empty", 400, code="invalid_prompt")
    task = await task_queue.add_task(
        prompt=req.prompt.strip(),
        target_count=req.target_count,
        aspect_ratio=resolve_aspect_ratio(req.size),
        enable_pro=spec.enable_pro,
        interval_seconds=req.interval_seconds,
        origin=req.origin or "queue",
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


@app.post("/v1/images/tasks/bulk/pause", tags=[_TAG_TASKS], summary="批量暂停所有 running/pending 任务",
          dependencies=[Depends(_require_api_key)])
async def task_bulk_pause() -> JSONResponse:
    n = 0
    for t in list(task_queue.list_tasks()):
        if t.get("kind") == "stream":
            continue
        if t.get("status") in ("running", "pending"):
            if await task_queue.pause_task(t["id"]):
                n += 1
    return JSONResponse({"paused": n})


@app.post("/v1/images/tasks/bulk/retry-failed", tags=[_TAG_TASKS],
          summary="只重试 failed 状态的任务（不动 cancelled / paused）",
          dependencies=[Depends(_require_api_key)])
async def task_bulk_retry_failed() -> JSONResponse:
    n = 0
    for t in list(task_queue.list_tasks()):
        if t.get("kind") == "stream":
            continue
        if t.get("status") == "failed":
            if await task_queue.retry_task(t["id"]):
                n += 1
    return JSONResponse({"retried": n})


@app.post("/v1/images/tasks/bulk/best-effort", tags=[_TAG_TASKS],
          summary="对所有满足条件的 failed 任务批量启用尽力模式",
          description=(
              "条件：status=failed 且（generated_count >= 2 或 generated_count*5 >= target_count）。"
              "保留已有进度，允许总尝试数追加 target_count*5 上限。"
          ),
          dependencies=[Depends(_require_api_key)])
async def task_bulk_best_effort() -> JSONResponse:
    n = 0
    for t in list(task_queue.list_tasks()):
        if t.get("kind") == "stream":
            continue
        status = t.get("status")
        gen = int(t.get("generated_count") or 0)
        tgt = int(t.get("target_count") or 0)
        partial = gen >= 2 or (tgt > 0 and gen * 5 >= tgt)
        eligible = (
            (status == "failed" and partial)
            or (status == "done" and gen < tgt and partial and int(t.get("attempt_cap") or 0) == 0)
        )
        if not eligible:
            continue
        if await task_queue.enable_best_effort(t["id"]):
            n += 1
    return JSONResponse({"enabled": n})


@app.post("/v1/images/tasks/bulk/resume", tags=[_TAG_TASKS],
          summary="批量恢复所有 paused/failed/cancelled 任务",
          dependencies=[Depends(_require_api_key)])
async def task_bulk_resume() -> JSONResponse:
    n = 0
    for t in list(task_queue.list_tasks()):
        if t.get("kind") == "stream":
            continue
        st = t.get("status")
        if st == "paused":
            if await task_queue.resume_task(t["id"]):
                n += 1
        elif st in ("failed", "cancelled"):
            if await task_queue.retry_task(t["id"]):
                n += 1
    return JSONResponse({"resumed": n})


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


@app.post("/v1/images/tasks/{task_id}/close", tags=[_TAG_TASKS],
          summary="关闭 failed 任务（标为 done，保留错误信息）",
          dependencies=[Depends(_require_api_key)])
async def task_close(task_id: str) -> JSONResponse:
    ok = await task_queue.close_task(task_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Only failed tasks can be closed")
    return JSONResponse({"ok": True})


@app.post("/v1/images/tasks/{task_id}/retry", tags=[_TAG_TASKS], summary="重试失败任务",
          description="将 failed 状态的任务重置为 pending，清空失败计数重新入队。",
          dependencies=[Depends(_require_api_key)])
async def task_retry(task_id: str) -> JSONResponse:
    ok = await task_queue.retry_task(task_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Task cannot be retried (not in failed state)")
    return JSONResponse({"ok": True})


@app.post("/v1/images/tasks/{task_id}/best-effort", tags=[_TAG_TASKS],
          summary="对 failed 任务启用尽力模式（保留进度，再尝试 target×5 次）",
          description=(
              "对 failed 任务启用尽力模式：保留已有的成功/被审/失败计数，将任务重新入队，"
              "允许总尝试次数追加 `target_count * 5` 上限。"
              "提前达到 target 张成功 → done；耗尽 cap 也判定为 done（视为已尽力）。"
          ),
          dependencies=[Depends(_require_api_key)])
async def task_best_effort(task_id: str) -> JSONResponse:
    ok = await task_queue.enable_best_effort(task_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Only failed tasks can enter best-effort mode")
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
    # 从 DB 批量查 session_id → model 映射，以及错误次数和被审次数（关联 task_queue）
    model_map: dict[str, str] = {}
    err_map: dict[str, int] = {}      # 该 session 在 image_logs 中 status='error' 的次数
    moderated_map: dict[str, int] = {}  # 来自 task_queue.moderated_count
    failed_count_map: dict[str, int] = {}  # 来自 task_queue.failed_count
    if log_db:
        try:
            with log_db._connect() as conn:
                rows = conn.execute(
                    "SELECT request_id, model FROM image_logs GROUP BY request_id"
                ).fetchall()
                model_map = {r[0]: r[1] for r in rows}
                err_rows = conn.execute(
                    "SELECT request_id, COUNT(*) FROM image_logs WHERE status='error' GROUP BY request_id"
                ).fetchall()
                err_map = {r[0]: r[1] for r in err_rows}
                tq_rows = conn.execute(
                    "SELECT session_id, moderated_count, failed_count FROM task_queue"
                ).fetchall()
                for r in tq_rows:
                    moderated_map[r[0]] = r[1] or 0
                    failed_count_map[r[0]] = r[2] or 0
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
            "moderated_count": moderated_map.get(d.name, 0),
            "failed_count": failed_count_map.get(d.name, 0) + err_map.get(d.name, 0),
        })
    return JSONResponse({"sessions": sessions, "total": len(sessions)})


@app.delete("/v1/images/file/{session_id}/{filename}", tags=[_TAG_GALLERY],
            summary="删除单张图片/视频文件",
            description="物理删除指定 session 下的单个媒体文件（与 Files 删除逻辑一致）。",
            dependencies=[Depends(_require_api_key)])
async def image_file_delete(session_id: str, filename: str) -> JSONResponse:
    safe_sid = Path(session_id).name
    safe_fn = Path(filename).name
    path = IMAGE_DIR / safe_sid / safe_fn
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        path.unlink()
    except Exception as exc:
        logger.exception("image file delete failed: sid=%s fn=%s", safe_sid, safe_fn)
        raise HTTPException(status_code=500, detail="Delete operation failed") from exc
    return JSONResponse({"ok": True, "session_id": safe_sid, "filename": safe_fn})


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
            raw_path = f"/v1/files/{'video' if is_vid else 'image'}/{sid}/{f.name}"
            result.append({
                "session_id": sid,
                "filename": f.name,
                "file_type": "video" if is_vid else "image",
                "url": base + _sign_file_url(raw_path),
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
}

# Image generation uses the "auto" quota bucket
_IMAGE_QUOTA_MODE = "auto"


def _parse_effort_limits(d: dict | None) -> dict | None:
    """解析 lowEffortRateLimits / highEffortRateLimits 嵌套对象。"""
    if not d or not isinstance(d, dict):
        return None
    result: dict = {}
    if (r := d.get("remainingQueries")) is not None:
        result["remaining_queries"] = int(r)
    if (t := d.get("totalQueries")) is not None:
        result["total_queries"] = int(t)
    if (w := d.get("waitTimeSeconds")) is not None:
        result["wait_time_seconds"] = int(w)
    return result or None


async def _fetch_quota(settings: Settings, mode_name: str = "auto") -> dict | None:
    """POST /rest/rate-limits for a mode, return parsed quota or None.

    注意：此函数接受 settings 参数，由调用方（通过 account_pool.acquire()）传入影子 settings。
    """
    try:
        body = await query_rate_limits(settings, model_name=mode_name)
        remaining = body.get("remainingQueries")
        if remaining is None:
            return None
        total = body.get("totalQueries") or remaining
        window = body.get("windowSizeSeconds") or 72000
        used = total - remaining
        pct = round(used / total * 100, 1) if total > 0 else 0.0
        result: dict = {
            "mode": mode_name,
            "remaining": int(remaining),
            "total": int(total),
            "used": int(used),
            "used_pct": pct,
            "window_seconds": int(window),
        }
        wait = body.get("waitTimeSeconds")
        if wait is not None:
            result["wait_time_seconds"] = int(wait)
        low = _parse_effort_limits(body.get("lowEffortRateLimits"))
        high = _parse_effort_limits(body.get("highEffortRateLimits"))
        if low is not None:
            result["low_effort"] = low
        if high is not None:
            result["high_effort"] = high
        logger.info("quota: mode=%s remaining=%d/%d used=%.1f%%", mode_name, int(remaining), int(total), pct)
        return result
    except Exception as exc:
        logger.warning("quota fetch failed: mode=%s error=%s", mode_name, exc)
        return None


@app.post("/v1/quota/chat", tags=[_TAG_QUOTA], summary="查询 Chat 模型配额",
          description="并发查询动态模型注册表中全部非图片模型的 rate-limits",
          dependencies=[Depends(_require_api_key)])
async def chat_quota(settings: Annotated[Settings, Depends(_settings)]) -> JSONResponse:
    if not settings.grok_cookie:
        return _error_response("GROK_COOKIE not configured", 400, code="missing_grok_cookie")
    specs = [s for s in get_model_specs() if not s.image_model]
    if not specs:
        return JSONResponse({"chat_quotas": []})

    with account_pool.acquire() as acq:
        async def _one(spec: ModelSpec) -> dict:
            base = {"model_id": spec.model_id, "mode_id": spec.mode_id, "label": spec.name}
            try:
                d = await query_rate_limits(acq.settings, model_name=spec.mode_id)
                remaining = int(d.get("remainingQueries") or 0)
                total = int(d.get("totalQueries") or remaining)
                window = int(d.get("windowSizeSeconds") or 7200)
                used = max(total - remaining, 0)
                pct = round(used / total * 100, 1) if total > 0 else 0.0
                logger.info("quota/chat: %s (mode=%s) remaining=%d/%d used=%.1f%%",
                            spec.model_id, spec.mode_id, remaining, total, pct)
                return {**base, "remaining": remaining, "total": total,
                        "used": used, "used_pct": pct, "window_seconds": window}
            except Exception as exc:
                return {**base, "error": str(exc)}

        out = await asyncio.gather(*[_one(s) for s in specs])
    return JSONResponse({"chat_quotas": list(out)})


@app.post("/v1/quota/image", tags=[_TAG_QUOTA], summary="探测图片生成额度",
          description=(
              "依次用多个候选 modelName 查询 `/rest/rate-limits`，找出 grok.com 图片额度的实际接口。\n\n"
              "返回 `candidates` 列表，每项含 `model_name`、`remaining`、`total`、`used_pct`（成功时），"
              "或 `error`、`status_code`（失败时）。`status_code=404` 表示该 modelName 无效。\n\n"
              "找到有效 modelName 后可在配置中记录供后续复用。"
          ),
          dependencies=[Depends(_require_api_key)])
async def image_quota(settings: Annotated[Settings, Depends(_settings)]) -> JSONResponse:
    with account_pool.acquire() as acq:
        result = await query_image_rate_limits(acq.settings)
    valid = [c for c in result["candidates"] if "remaining" in c]
    result["best"] = valid[0] if valid else None
    return JSONResponse(result)


class _ChatImagineRequest(BaseModel):
    prompt: str
    image_count: int = 2
    mode_id: str = "fast"  # grok 网页端实际使用 "fast"，不是 LLM 模型名
    aspect_ratio: str = "1:1"  # 注入到 prompt 头部供 LLM 选 orientation


@app.post("/v1/images/chat-imagine", tags=[_TAG_OPENAI],
          summary="通过 Chat 端口让 LLM 改写 prompt 后生成图片（绕过严格审核）",
          description=(
              "POST 一个 prompt，Grok LLM 会自行改写成更安全的版本后生成图片。\n"
              "- 比 imagine WS 直接调用慢（30-90s），但更不易被审核拦截\n"
              "- imageGenerationCount 上限 4\n"
              "- 失败会返回 `code:chat_no_image`（LLM 也拒绝了）"
          ),
          dependencies=[Depends(_require_api_key)])
async def chat_imagine_endpoint(
    req: _ChatImagineRequest,
    settings: Annotated[Settings, Depends(_settings)],
) -> JSONResponse:
    if not req.prompt or not req.prompt.strip():
        return _error_response("prompt is required", 400)
    sess_id = str(uuid.uuid4())
    t0 = time.monotonic()
    summary = ""
    _chat_imagine_label = ""
    with account_pool.acquire(model_id=req.mode_id) as acq:
        _chat_imagine_label = acq.label
        try:
            urls, summary = await chat_imagine(
                acq.settings, message=req.prompt, mode_id=req.mode_id,
                image_count=req.image_count, aspect_ratio=req.aspect_ratio,
            )
        except GrokClientError as exc:
            acq.mark_failure(exc.code or "upstream_5xx", retry_after=exc.retry_after)
            log_db.log_image(
                request_id=sess_id, model=req.mode_id, prompt=req.prompt,
                image_count=0, aspect_ratio="", source="chat-imagine",
                status="error", duration_ms=int((time.monotonic() - t0) * 1000), error=str(exc),
                account_label=acq.label,
            )
            return _error_response(str(exc), exc.status_code, code=exc.code or "upstream_error")
        except Exception as exc:
            acq.mark_failure("upstream_5xx")
            logger.exception("chat-imagine failed")
            log_db.log_image(
                request_id=sess_id, model=req.mode_id, prompt=req.prompt,
                image_count=0, aspect_ratio="", source="chat-imagine",
                status="error", duration_ms=int((time.monotonic() - t0) * 1000), error=str(exc),
                account_label=acq.label,
            )
            return _error_response("Internal server error", 500)
        full = []
        for u in urls:
            if u.startswith("http"):
                full.append(u)
            else:
                full.append(f"https://assets.grok.com/{u.lstrip('/')}")
        # 落盘到 data/images/{sess_id}/，让"图库"能扫到这个 session
        from .grok_client import _init_session as _grok_init_session, stream_grok_asset
        saved_paths: list[str] = []
        try:
            sess_dir = _grok_init_session(
                sess_id, prompt=req.prompt, source="chat-imagine", aspect_ratio="",
            )
            for idx, full_url in enumerate(full):
                try:
                    m = re.search(r"assets\.grok\.com/(.+)$", full_url)
                    if not m:
                        continue
                    key = m.group(1)
                    ext = os.path.splitext(key.split("/")[-1])[1] or ".jpg"
                    fn = f"chat-{idx:02d}{ext}"
                    fp = sess_dir / fn
                    _ct, gen = await stream_grok_asset(acq.settings, key)
                    with open(fp, "wb") as f:
                        async for chunk in gen:
                            f.write(chunk)
                    saved_paths.append(f"{sess_id}/{fn}")
                except Exception as save_exc:
                    logger.warning("chat-imagine save failed for %s: %s", full_url[:100], save_exc)
        except Exception:
            logger.exception("chat-imagine session_dir / download failed")
    log_db.log_image(
        request_id=sess_id, model=req.mode_id, prompt=req.prompt,
        image_paths=saved_paths or full, image_count=len(full),
        aspect_ratio="", source="chat-imagine",
        status="success", duration_ms=int((time.monotonic() - t0) * 1000),
        account_label=_chat_imagine_label,
    )
    return JSONResponse({
        "image_urls": full,
        "image_count": len(full),
        "session_id": sess_id,
        "model_summary": summary,
    })


@app.post("/v1/quota", tags=[_TAG_QUOTA], summary="查询额度剩余",
          description=(
              "并发请求 Grok `/rest/rate-limits` 查询 auto / fast / think 三种模式的额度。\n\n"
              "**返回**：每个模式的 `remaining`、`total`、`used`、`used_pct`（百分比）、`window_seconds`（窗口时长）。\n\n"
              "当 auto 模式使用率 ≥ 90% 时 `image_blocked=true`，建议暂停生图。"
          ),
          dependencies=[Depends(_require_api_key)])
async def quota_check(settings: Annotated[Settings, Depends(_settings)]) -> JSONResponse:
    if not settings.grok_cookie:
        return _error_response("GROK_COOKIE not configured", 400, code="missing_grok_cookie")

    import asyncio, time as _time
    t0 = _time.monotonic()
    with account_pool.acquire() as acq:
        results = await asyncio.gather(*[
            _fetch_quota(acq.settings, mode) for mode in _QUOTA_MODES
        ])
    quotas = {r["mode"]: r for r in results if r}

    # 用 auto 模式判断是否超限 90%
    auto = quotas.get("auto")
    image_blocked = False
    if auto and auto["total"] > 0:
        image_blocked = auto["used_pct"] >= 90.0

    ms = int((_time.monotonic() - t0) * 1000)
    detail_parts = [f"{m}:{q['remaining']}/{q['total']}" for m, q in quotas.items() if q]
    log_db.log_system(
        event_type="quota",
        status="success",
        duration_ms=ms,
        detail=" | ".join(detail_parts),
    )
    logger.info("quota check: %s in %dms", " | ".join(detail_parts) if detail_parts else "no data", ms)

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
        is_cf = code in {"cloudflare_challenge", "upstream_403"}
        monitor.record_failure(status=exc.status_code, summary=str(exc), cloudflare=is_cf)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    update_kwargs: dict[str, object] = {
        "grok_cookie": result.cookie,
        "grok_user_agent": result.user_agent,
        "grok_browser": result.browser,
    }
    if result.statsig_id:
        update_kwargs["grok_statsig_id"] = result.statsig_id
    settings_store.update(**update_kwargs)
    monitor.record_success(status=int(smoke["status_code"]))
    return JSONResponse({"ok": True, "message": f"cURL 导入和冒烟成功，状态码 {smoke['status_code']}。"})


@app.post("/admin/config", tags=[_TAG_ADMIN], summary="更新运行配置",
          description=(
              "逐字段更新配置（留空字段不修改）。`Content-Type: application/x-www-form-urlencoded`。\n\n"
              "**可更新字段**：`api_key`、`grok_cookie`、`grok_user_agent`、`grok_browser`（curl_cffi 指纹）、`grok_proxy`、`log_retention_days`、`default_image_model`。"
          ),
          dependencies=[Depends(_require_api_key)])
async def update_config(
    api_key: Annotated[str, Form()] = "",
    grok_cookie: Annotated[str, Form()] = "",
    grok_user_agent: Annotated[str, Form()] = "",
    grok_browser: Annotated[str, Form()] = "",
    grok_proxy: Annotated[str, Form()] = "",
    flaresolverr_url: Annotated[str, Form()] = "",
    log_retention_days: Annotated[str, Form()] = "",
    default_image_model: Annotated[str, Form()] = "",
) -> JSONResponse:
    patch: dict[str, object] = {}
    new_api_key: str | None = None
    if api_key.strip():
        new_api_key = api_key.strip()
        patch["api_key"] = new_api_key
    if grok_cookie.strip():
        patch["grok_cookie"] = grok_cookie.strip()
    if grok_user_agent.strip():
        patch["grok_user_agent"] = grok_user_agent.strip()
    if grok_browser.strip():
        patch["grok_browser"] = grok_browser.strip()
    patch["grok_proxy"] = grok_proxy.strip()  # 允许清空（空字符串=禁用代理）
    if flaresolverr_url.strip():
        patch["flaresolverr_url"] = flaresolverr_url.strip()
    if log_retention_days.strip():
        try:
            patch["log_retention_days"] = int(log_retention_days.strip())
        except ValueError:
            pass
    if default_image_model.strip():
        patch["default_image_model"] = default_image_model.strip()
    if patch:
        old_api_key = settings_store.get().api_key
        settings_store.update(**patch)
        safe_keys = {k: ("***" if k in ("api_key", "grok_cookie") else v) for k, v in patch.items()}
        logger.info("config updated: %s", safe_keys)
        # api_key 轮换：撤销所有 cookie session，强制所有浏览器重新登录。
        # 这里在 update 之后做，确保配置已落盘。
        if new_api_key is not None and new_api_key != old_api_key:
            revoked = session_store.revoke_all()
            logger.info("api_key rotated; revoked %d active session(s)", revoked)
        # 凭证变更：同步 default 账号（保留 enabled/priority/weight 及运行时状态）
        # grok_proxy 总在 patch 里（允许清空），只有值真的变化时才算凭证变更
        _EXPLICIT_CREDENTIAL_KEYS = {"grok_cookie", "grok_user_agent", "grok_browser", "grok_statsig_id"}
        _credential_changed = bool(_EXPLICIT_CREDENTIAL_KEYS & patch.keys())
        if not _credential_changed and "grok_proxy" in patch:
            # grok_proxy 清空/赋值时，与旧值比较，真的不同才触发同步
            fresh_settings = settings_store.get()
            _credential_changed = (patch["grok_proxy"] != fresh_settings.grok_proxy)
        if _credential_changed:
            fresh_settings = settings_store.get()
            account_pool.import_from_settings(fresh_settings, force_refresh_default=True)
            logger.info("account_pool: default account synced from settings")
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
    account_label: str = "",
) -> JSONResponse:
    rows, total = log_db.query(
        log_type=log_type, search=search, offset=offset, limit=limit,
        from_ts=from_ts, to_ts=to_ts, account_label=account_label,
    )
    return JSONResponse({"total": total, "offset": offset, "limit": limit, "data": rows})


@app.post("/admin/cleanup-logs", tags=[_TAG_ADMIN], summary="清理过期日志",
          description="删除超过 `log_retention_days`（默认 90 天）的历史日志记录，返回删除条数。",
          dependencies=[Depends(_require_api_key)])
async def cleanup_logs(settings: Annotated[Settings, Depends(_settings)]) -> JSONResponse:
    deleted = log_db.cleanup(settings.log_retention_days)
    return JSONResponse({"ok": True, "deleted": deleted})


# ---------------------------------------------------------------------------
# Model management API
# ---------------------------------------------------------------------------

@app.get("/admin/models", tags=[_TAG_ADMIN], summary="获取模型列表",
         description="返回当前配置的完整模型列表。",
         dependencies=[Depends(_require_api_key)])
async def admin_models_get(settings: Annotated[Settings, Depends(_settings)]) -> JSONResponse:
    return JSONResponse({"models": list(settings.chat_models)})


class _ModelVerifyRequest(BaseModel):
    mode_id: str = ""


@app.post("/admin/models/verify", tags=[_TAG_ADMIN], summary="验证模型 modeId 是否有效",
          description="对指定 modeId 调用 /rest/rate-limits，零消耗验证模型是否在线可用。",
          dependencies=[Depends(_require_api_key)])
async def admin_models_verify(
    req: _ModelVerifyRequest,
    settings: Annotated[Settings, Depends(_settings)],
) -> JSONResponse:
    mode_id = req.mode_id
    if not mode_id.strip():
        raise HTTPException(status_code=400, detail="mode_id 不能为空")
    with account_pool.acquire() as acq:
        try:
            raw = await query_rate_limits(acq.settings, model_name=mode_id.strip())
            remaining = raw.get("remainingQueries")
            total = raw.get("totalQueries")
            wait = raw.get("waitTimeSeconds")
            return JSONResponse({
                "ok": True,
                "mode_id": mode_id,
                "remaining": remaining,
                "total": total,
                "wait_seconds": wait,
            })
        except Exception as e:
            acq.mark_failure("upstream_5xx")
            return JSONResponse({"ok": False, "mode_id": mode_id, "error": str(e)})


class _ModelsUpdateRequest(BaseModel):
    models: list[dict]


@app.post("/admin/models", tags=[_TAG_ADMIN], summary="更新模型列表",
          description="全量替换模型列表并持久化到 mini.toml。每项需要 `id`、`mode_id`、`name`，可选 `image_model`、`enable_pro`。",
          dependencies=[Depends(_require_api_key)])
async def admin_models_update(req: _ModelsUpdateRequest) -> JSONResponse:
    cleaned = [
        {
            "id": str(m.get("id", "")).strip(),
            "mode_id": str(m.get("mode_id", "")).strip(),
            "name": str(m.get("name", m.get("id", ""))).strip(),
            "image_model": bool(m.get("image_model", False)),
            "enable_pro": bool(m.get("enable_pro", False)),
        }
        for m in req.models
        if isinstance(m, dict) and str(m.get("id", "")).strip() and str(m.get("mode_id", "")).strip()
    ]
    if not cleaned:
        raise HTTPException(status_code=400, detail="模型列表不能为空")
    settings_store.update(chat_models=tuple(cleaned))
    logger.info("models: registry updated with %d models: %s", len(cleaned), [m.get("id") for m in cleaned])
    _apply_models(settings_store.get())
    return JSONResponse({"ok": True, "count": len(cleaned)})


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
        "user_agent": settings.grok_user_agent or "",
        "proxy": settings.grok_proxy or "",
        "proxy_configured": bool(settings.grok_proxy),
        "flaresolverr_url": settings.flaresolverr_url or "",
        "log_retention_days": settings.log_retention_days,
        "default_image_model": settings.default_image_model,
    }


@app.post("/admin/dashboard", tags=[_TAG_ADMIN], summary="Dashboard 汇总",
          description="一次性返回 Dashboard 所需全部数据：配额、任务统计、日志统计、运行状态。",
          dependencies=[Depends(_require_api_key)])
async def admin_dashboard(settings: Annotated[Settings, Depends(_settings)]) -> JSONResponse:
    # 按动态模型注册表查询，每个 mode_id 只查一次
    chat_specs = [s for s in get_model_specs() if not s.image_model]
    seen_modes: dict[str, ModelSpec] = {}
    for s in chat_specs:
        if s.mode_id not in seen_modes:
            seen_modes[s.mode_id] = s
    with account_pool.acquire() as acq:
        quota_results = await asyncio.gather(*[_fetch_quota(acq.settings, mid) for mid in seen_modes])
        img_quota_result = await query_image_rate_limits(acq.settings)
    model_quotas: list[dict] = []
    for (mode_id, spec), q in zip(seen_modes.items(), quota_results):
        if q:
            entry = {"model_id": spec.model_id, "mode_id": mode_id, "name": spec.name}
            entry.update({k: v for k, v in q.items() if k != "mode"})
            model_quotas.append(entry)
        else:
            model_quotas.append({"model_id": spec.model_id, "mode_id": mode_id, "name": spec.name, "error": "fetch failed"})
    ok_count = sum(1 for q in model_quotas if "error" not in q)
    logger.info("dashboard: quota for %d/%d models ok", ok_count, len(model_quotas))
    log_stats = log_db.stats()
    task_stats = task_queue.stats()
    all_tasks = task_queue.list_tasks()
    total_moderated = sum(t.get("moderated_count", 0) for t in all_tasks)
    recent_tasks = sorted(
        [t for t in all_tasks if t.get("status") in ("pending", "running", "paused", "failed")],
        key=lambda t: t.get("priority", 999),
    )[:8]
    snap = monitor.snapshot()
    img_quota_valid = [c for c in img_quota_result["candidates"] if "remaining" in c]
    image_quota = img_quota_valid[0] if img_quota_valid else None
    return JSONResponse({
        "version": app.version,
        "model_quotas": model_quotas,
        "image_quota": image_quota,
        "tasks": {**task_stats, "total_moderated": total_moderated},
        "recent_tasks": recent_tasks,
        "logs": log_stats,
        "monitor": {
            "total_requests": snap.total_requests,
            "success_count": snap.success_count,
            "failure_count": snap.failure_count,
            "cloudflare_challenge": snap.cloudflare_challenge,
            "recent_error": snap.recent_error_summary,
            "per_account": {
                label: {
                    "total_requests": m.total_requests,
                    "success_count": m.success_count,
                    "failure_count": m.failure_count,
                    "recent_upstream_status": m.recent_upstream_status,
                    "recent_error_summary": m.recent_error_summary,
                }
                for label, m in snap.per_account.items()
            },
        },
        "cookie_configured": bool(settings.grok_cookie),
        "proxy_configured": bool(settings.grok_proxy),
        "accounts": [asdict(a) for a in account_pool.list_accounts()],
    })


@app.post("/v1/grok/assets", tags=[_TAG_FILES], summary="列出 Grok Files",
          description="分页拉取 Grok 云端文件列表（图片/视频）。",
          dependencies=[Depends(_require_api_key)])
# 原 GET 改 POST 防 CSRF（与 Wave 2 同批）：
# 无路径参数 + 触发上游 HTTP + 写 DB + 消耗配额，风险最高。
# FastAPI POST 同样支持 query 参数，前端无需改参数风格。
async def grok_assets_list(
    settings: Annotated[Settings, Depends(_settings)],
    page_token: str = "",
    page_size: int = 50,
) -> JSONResponse:
    with account_pool.acquire() as acq:
        try:
            data = await list_grok_assets(acq.settings, page_token=page_token, page_size=min(page_size, 100))
            # 顺便缓存到本地 DB（增量发现）
            if data.get("assets"):
                log_db.upsert_grok_assets(data["assets"])
            return JSONResponse(data)
        except GrokClientError as exc:
            acq.mark_failure(exc.code or "upstream_5xx", retry_after=exc.retry_after)
            return _error_response(str(exc), exc.status_code, code=exc.code or "upstream_error")
        except Exception:
            acq.mark_failure("upstream_5xx")
            logger.exception("grok assets list failed")
            return _error_response("Internal server error", 500)


@app.post("/v1/grok/assets/{asset_id}/delete", tags=[_TAG_FILES], summary="删除 Grok File",
          description="从 Grok 云端删除指定文件（仅删云端，本地文件保留；DB 标记 cloud_deleted_at）。",
          dependencies=[Depends(_require_api_key)])
async def grok_asset_delete(
    asset_id: str,
    settings: Annotated[Settings, Depends(_settings)],
) -> JSONResponse:
    with account_pool.acquire() as acq:
        try:
            confirmed = await delete_grok_asset(acq.settings, asset_id)
            if confirmed:
                log_db.mark_asset_cloud_deleted(asset_id)  # 仅在 API 200 后才标记
            return JSONResponse({"ok": True, "cloud_deleted": confirmed})
        except GrokClientError as exc:
            acq.mark_failure(exc.code or "upstream_5xx", retry_after=exc.retry_after)
            return _error_response(str(exc), exc.status_code, code=exc.code or "upstream_error")
        except Exception:
            acq.mark_failure("upstream_5xx")
            logger.exception("grok asset delete failed")
            return _error_response("Internal server error", 500)


@app.get("/v1/grok/assets/download", tags=[_TAG_FILES], summary="下载 Grok File",
         description=(
             "通过服务端代理从 assets.grok.com 流式拉取文件。\n\n"
             "鉴权：Header `Authorization: Bearer xxx` / `X-Api-Key: xxx` / 浏览器 HttpOnly cookie 三选一。\n\n"
             "**已移除** `?api_key=xxx` query 鉴权（避免 access log / DevTools / 截图泄露），"
             "前端浏览器走 cookie 通道；其它客户端用 Header。\n\n"
             "查询参数：\n"
             "- `inline=1`: 不触发浏览器下载（去掉 Content-Disposition），适合 <img src> 渐进式渲染。\n"
             "- `inline=0`(默认): 加 Content-Disposition: attachment 触发浏览器保存。"
         ),
         dependencies=[Depends(_require_api_key)])
async def grok_asset_download(
    settings: Annotated[Settings, Depends(_settings)],
    key: str = "",
    filename: str = "file",
    inline: int = 0,
) -> StreamingResponse:
    if not key:
        return JSONResponse({"error": "key is required"}, status_code=400)
    # 防御：拒绝 prompt:// / scheme:// 等伪 key（LLM 透传产物或意外字符串）
    if "://" in key or key.startswith("prompt:") or key.startswith("/"):
        return JSONResponse({"error": "invalid key format"}, status_code=400)
    with account_pool.acquire() as acq:
        try:
            content_type, gen = await stream_grok_asset(acq.settings, key)
            headers: dict[str, str] = {
                "Cache-Control": "private, max-age=43200",  # 12h，与 Grok CDN 一致
            }
            if not inline:
                safe_name = filename.replace('"', "_")
                headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
            return StreamingResponse(gen, media_type=content_type, headers=headers)
        except GrokClientError as exc:
            acq.mark_failure(exc.code or "upstream_5xx", retry_after=exc.retry_after)
            return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
        except Exception:
            acq.mark_failure("upstream_5xx")
            logger.exception("grok asset download failed")
            return JSONResponse({"error": "Internal server error"}, status_code=500)


class _SaveBatchItem(BaseModel):
    key: str
    filename: str
    size_bytes: int = 0


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
    skipped = []
    errors = []
    with account_pool.acquire() as acq:
        for item in req.files:
            try:
                path, was_skipped = await save_grok_asset_local(acq.settings, item.key, item.filename, item.size_bytes)
                entry = {"key": item.key, "filename": item.filename, "path": path}
                if was_skipped:
                    skipped.append(entry)
                else:
                    saved.append(entry)
            except GrokClientError as exc:
                errors.append({"key": item.key, "filename": item.filename, "error": str(exc)})
            except Exception:
                logger.exception("save local asset failed: key=%s", item.key)
                errors.append({"key": item.key, "filename": item.filename, "error": "Internal server error"})
    return JSONResponse({"saved": saved, "skipped": skipped, "errors": errors})


# ---------------------------------------------------------------------------
# Admin: 账号管理 (Account Pool)
# ---------------------------------------------------------------------------


class _AccountUpsertRequest(BaseModel):
    label: str
    # cookie 可空：仅编辑现有账号时允许（服务端保留旧值）；新建时仍校验非空
    cookie: str = ""
    user_agent: str = ""
    browser: str = "chrome142"
    proxy: str = ""
    statsig_id: str = ""
    # enabled = None 表示"未指定"：新建默认启用，编辑保留现有 enabled 状态。
    # 防止 edit modal 不传 enabled 时把禁用账号悄然启用。
    enabled: bool | None = None
    priority: int = 1
    weight: int = 10


class _AccountEnabledRequest(BaseModel):
    enabled: bool


class _AccountImportCurlRequest(BaseModel):
    curl: str
    label: str
    priority: int = 1
    weight: int = 10


@app.get("/admin/accounts", tags=[_TAG_ADMIN], summary="列出所有 Grok 账号",
         description="返回账号池中所有账号的状态快照，包括启用状态、优先级、配额统计、最近使用时间等。",
         dependencies=[Depends(_require_api_key)])
async def admin_list_accounts() -> JSONResponse:
    accounts = account_pool.list_accounts()
    # 附加每账号的配额摘要（不破坏现有字段）
    quota_map = {q["label"]: q["quotas"] for q in account_pool.list_account_quotas()}
    account_dicts = []
    for a in accounts:
        d = asdict(a)
        d["quota_cache"] = quota_map.get(a.label, {})
        account_dicts.append(d)
    return JSONResponse({"accounts": account_dicts})


@app.post("/admin/accounts", tags=[_TAG_ADMIN], summary="新增 / 更新 Grok 账号",
          description="以 label 为主键，新增或更新账号配置。cookie 字段必填；其余留空则保持默认值。",
          dependencies=[Depends(_require_api_key)])
async def admin_upsert_account(req: _AccountUpsertRequest) -> JSONResponse:
    label = req.label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="label 不能为空")
    cookie = req.cookie.strip()
    existing = account_pool.get_account(label)
    if not cookie:
        # 编辑现有账号且 cookie 留空 → 保留旧 cookie 与凭证字段（仅改 priority/weight/enabled）
        if existing is None:
            raise HTTPException(status_code=400, detail="cookie 不能为空（新建账号必须提供）")
        cookie = existing.cookie
    # enabled 字段三态：True/False 显式覆盖；None 时新建默认 True，编辑保留 existing.enabled
    if req.enabled is None:
        enabled = existing.enabled if existing else True
    else:
        enabled = req.enabled
    acc = Account(
        label=label,
        cookie=cookie,
        user_agent=req.user_agent.strip() or (existing.user_agent if existing else ""),
        browser=req.browser.strip() or (existing.browser if existing else "chrome142"),
        proxy=req.proxy.strip() or (existing.proxy if existing else ""),
        statsig_id=req.statsig_id.strip() or (existing.statsig_id if existing else ""),
        enabled=enabled,
        priority=req.priority,
        weight=req.weight,
    )
    account_pool.upsert_account(acc)
    action = "updated" if existing else "created"
    logger.info("admin: %s account label=%r", action, label)
    return JSONResponse({"ok": True, "label": label, "action": action})


@app.delete("/admin/accounts/{label}", tags=[_TAG_ADMIN], summary="删除 Grok 账号",
            description="从账号池中移除指定账号。账号不存在时返回 404。",
            dependencies=[Depends(_require_api_key)])
async def admin_delete_account(label: str) -> JSONResponse:
    ok = account_pool.delete_account(label)
    if not ok:
        raise HTTPException(status_code=404, detail=f"账号 {label!r} 不存在")
    logger.info("admin: deleted account label=%r", label)
    return JSONResponse({"ok": True, "label": label})


@app.post("/admin/accounts/{label}/enabled", tags=[_TAG_ADMIN], summary="启用 / 禁用账号",
          description="切换指定账号的启用状态。禁用后状态变为 `manually_disabled`，不参与请求调度。",
          dependencies=[Depends(_require_api_key)])
async def admin_set_account_enabled(label: str, req: _AccountEnabledRequest) -> JSONResponse:
    ok = account_pool.set_enabled(label, req.enabled)
    if not ok:
        raise HTTPException(status_code=404, detail=f"账号 {label!r} 不存在")
    action = "enabled" if req.enabled else "disabled"
    logger.info("admin: account %s label=%r", action, label)
    return JSONResponse({"ok": True, "label": label, "enabled": req.enabled})


@app.post("/admin/accounts/import-curl", tags=[_TAG_ADMIN], summary="从 cURL 导入新账号",
          description=(
              "解析 Chrome/Edge 复制的 cURL 命令，提取 Cookie、UA、浏览器指纹，"
              "以指定 label 注册为新账号（不做冒烟验证，速度快）。\n\n"
              "label 已存在时会覆盖更新。"
          ),
          dependencies=[Depends(_require_api_key)])
async def admin_import_curl_as_account(req: _AccountImportCurlRequest) -> JSONResponse:
    """复用现有 curl_import.parse_grok_curl，但落到账号表而不是 settings。"""
    if not req.label.strip():
        raise HTTPException(status_code=400, detail="label 不能为空")
    if not req.curl.strip():
        raise HTTPException(status_code=400, detail="curl 不能为空")
    try:
        result = parse_grok_curl(req.curl)
    except CurlImportError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    acc = Account(
        label=req.label.strip(),
        cookie=result.cookie,
        user_agent=result.user_agent,
        browser=result.browser,
        proxy="",
        statsig_id=result.statsig_id,
        enabled=True,
        priority=req.priority,
        weight=req.weight,
    )
    account_pool.upsert_account(acc)
    logger.info("admin: import-curl account label=%r browser=%s", acc.label, acc.browser)
    return JSONResponse({
        "ok": True,
        "label": acc.label,
        "browser": acc.browser,
        "cookie_masked": acc.cookie[:8] + "..." + acc.cookie[-8:] if len(acc.cookie) > 16 else "***",
    })


def run() -> None:
    import uvicorn

    settings = settings_store.get()
    uvicorn.run(
        "mini_grok_api.main:_top_app",
        host=settings.server_host,
        port=settings.server_port,
        reload=False,
    )


if __name__ == "__main__":
    run()
