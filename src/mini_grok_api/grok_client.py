"""Grok Web 文本聊天客户端。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from curl_cffi.requests import AsyncSession

from .config import Settings

logger = logging.getLogger(__name__)

CHAT_URL = "https://grok.com/rest/app-chat/conversations/new"
SKILLS_URL = "https://grok.com/rest/skills"
WS_IMAGINE_URL = "wss://grok.com/ws/imagine/listen"

IMAGE_DIR = Path("data/images")

_SIZE_TO_RATIO: dict[str, str] = {
    "1280x720": "16:9", "16:9": "16:9",
    "720x1280": "9:16", "9:16": "9:16",
    "1792x1024": "3:2", "3:2": "3:2",
    "1024x1792": "2:3", "2:3": "2:3",
    "1024x1024": "1:1", "1:1": "1:1",
}


def resolve_aspect_ratio(size: str) -> str:
    return _SIZE_TO_RATIO.get(size, "1:1")


class GrokClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        body: str = "",
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.code = code


@dataclass(frozen=True, slots=True)
class GrokTextDelta:
    content: str
    done: bool = False


def build_chat_payload(message: str, mode_id: str) -> dict[str, Any]:
    return {
        "collectionIds": [],
        "connectors": [],
        "deviceEnvInfo": {
            "darkModeEnabled": False,
            "devicePixelRatio": 2,
            "screenHeight": 1329,
            "screenWidth": 2056,
            "viewportHeight": 1083,
            "viewportWidth": 2056,
        },
        "disableMemory": True,
        "disableSearch": False,
        "disableSelfHarmShortCircuit": False,
        "disableTextFollowUps": False,
        "enableImageGeneration": True,
        "enableImageStreaming": True,
        "enableSideBySide": True,
        "fileAttachments": [],
        "forceConcise": False,
        "forceSideBySide": False,
        "imageAttachments": [],
        "imageGenerationCount": 2,
        "isAsyncChat": False,
        "message": message,
        "modeId": mode_id,
        "responseMetadata": {},
        "returnImageBytes": False,
        "returnRawGrokInXaiRequest": False,
        "searchAllConnectors": False,
        "sendFinalMetadata": True,
        "temporary": True,
        "toolOverrides": {
            "gmailSearch": False,
            "googleCalendarSearch": False,
            "outlookSearch": False,
            "outlookCalendarSearch": False,
            "googleDriveSearch": False,
        },
    }


def classify_line(line: str | bytes) -> tuple[str, str]:
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    text = line.strip()
    if not text:
        return "skip", ""
    if text.startswith("data:"):
        data = text[5:].strip()
        return ("done", "") if data == "[DONE]" else ("data", data)
    if text.startswith("{"):
        return "data", text
    return "skip", ""


def parse_text_delta(data: str) -> list[GrokTextDelta]:
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return []
    response = ((obj.get("result") or {}).get("response") or {})
    if not isinstance(response, dict):
        return []
    if response.get("isSoftStop") or response.get("finalMetadata"):
        return [GrokTextDelta("", done=True)]
    token = response.get("token")
    if token is None or response.get("isThinking") is True:
        return []
    if response.get("messageTag") != "final":
        return []
    return [GrokTextDelta(str(token))]


def _contains_cloudflare_challenge(status_code: int, body: str) -> bool:
    lower = body.lower()
    return status_code in {401, 403, 429} and (
        "cloudflare" in lower
        or "cf-mitigated" in lower
        or "challenge" in lower
        or "cf_clearance" in lower
    )


_STATSIG_ID = (
    "ZTpUeXBlRXJyb3I6IENhbm5vdCByZWFkIHByb3BlcnRpZXMgb2YgdW5kZWZpbmVkIChyZWFkaW5nICdjaGls"
    "ZE5vZGVzJyk="
)
_BAGGAGE = (
    "sentry-environment=production,"
    "sentry-release=d6add6fb0460641fd482d767a335ef72b9b6abb8,"
    "sentry-public_key=b311e0f2690c81f25e2c4cf6d4f7ce1c"
)


def _client_hints(browser: str, user_agent: str) -> dict[str, str]:
    b = browser.lower()
    u = user_agent.lower()
    is_chromium = any(k in b for k in ("chrome", "chromium", "edge", "brave")) or any(
        k in u for k in ("chrome", "chromium", "edg")
    )
    if not is_chromium or "firefox" in u or ("safari" in u and "chrome" not in u):
        return {}

    # 从 UA 中提取主版本号
    version_match = re.search(r"(?:edg|chrome)/(\d{2,3})", u)
    if not version_match:
        version_match = re.search(r"(\d{2,3})", f"{browser} {user_agent}")
    version = version_match.group(1) if version_match else "136"

    if "edge" in b or "edg/" in u:
        brand = "Microsoft Edge"
        full_version = f"{version}.0.0.0"
    elif "brave" in b:
        brand = "Brave"
        full_version = f"{version}.0.0.0"
    elif "chromium" in b:
        brand = "Chromium"
        full_version = f"{version}.0.0.0"
    else:
        brand = "Google Chrome"
        full_version = f"{version}.0.0.0"

    ua_lower = user_agent.lower()
    if "windows" in ua_lower:
        platform, platform_version = "Windows", "10.0.0"
    elif "mac os x" in ua_lower or "macintosh" in ua_lower:
        platform, platform_version = "macOS", "15.0.0"
    elif "android" in ua_lower:
        platform, platform_version = "Android", "14.0.0"
    elif "linux" in ua_lower:
        platform, platform_version = "Linux", "6.5.0"
    else:
        platform, platform_version = "Windows", "10.0.0"

    mobile = "?1" if "mobile" in ua_lower else "?0"
    arch = "arm" if ("arm" in ua_lower or "apple" in ua_lower or "mac os x" in ua_lower) else "x86"
    bitness = "64"

    full_version_list = f'"{brand}";v="{full_version}", "Not.A/Brand";v="8.0.0.0", "Chromium";v="{full_version}"'

    return {
        "Sec-Ch-Ua": f'"{brand}";v="{version}", "Not.A/Brand";v="8", "Chromium";v="{version}"',
        "Sec-Ch-Ua-Mobile": mobile,
        "Sec-Ch-Ua-Model": '""',
        "Sec-Ch-Ua-Platform": f'"{platform}"',
        "Sec-Ch-Ua-Platform-Version": f'"{platform_version}"',
        "Sec-Ch-Ua-Arch": f'"{arch}"',
        "Sec-Ch-Ua-Bitness": f'"{bitness}"',
        "Sec-Ch-Ua-Full-Version": f'"{full_version}"',
        "Sec-Ch-Ua-Full-Version-List": full_version_list,
        "DNT": "1",
    }


def _headers(settings: Settings) -> dict[str, str]:
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Baggage": _BAGGAGE,
        "Content-Type": "application/json",
        "Origin": "https://grok.com",
        "Priority": "u=1, i",
        "Referer": "https://grok.com/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": settings.grok_user_agent,
        "x-statsig-id": _STATSIG_ID,
        "x-xai-request-id": str(uuid.uuid4()),
        "Cookie": settings.grok_cookie,
    }
    headers.update(_client_hints(settings.grok_browser, settings.grok_user_agent))
    return headers


def _merge_safe_headers(base: dict[str, str], extra: dict[str, str] | None) -> dict[str, str]:
    if not extra:
        return base
    blocked = {"host", "content-length"}
    for name, value in extra.items():
        canonical = name.lower()
        if canonical in blocked:
            continue
        if canonical == "cookie":
            base["Cookie"] = value
        else:
            base[name] = value
    return base


def _session_kwargs(settings: Settings) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if settings.grok_browser:
        kwargs["impersonate"] = settings.grok_browser
    if settings.grok_proxy:
        scheme = urlparse(settings.grok_proxy).scheme.lower()
        proxy = settings.grok_proxy.split("#")[0].strip()  # 去掉 #label 注释片段
        if scheme == "socks":
            proxy = "socks5h://" + proxy[len("socks://"):]
        elif scheme == "socks5":
            proxy = "socks5h://" + proxy[len("socks5://"):]
        if scheme.startswith("socks"):
            kwargs["proxy"] = proxy
        else:
            kwargs["proxies"] = {"http": proxy, "https": proxy}
    return kwargs


async def stream_chat(
    settings: Settings,
    *,
    message: str,
    mode_id: str,
) -> AsyncGenerator[GrokTextDelta, None]:
    if not settings.grok_cookie:
        raise GrokClientError(
            "GROK_COOKIE is not configured",
            status_code=400,
            code="missing_grok_cookie",
        )

    payload = json.dumps(build_chat_payload(message, mode_id), ensure_ascii=False).encode("utf-8")
    async with AsyncSession(**_session_kwargs(settings)) as session:
        try:
            response = await session.post(
                CHAT_URL,
                headers=_headers(settings),
                data=payload,
                timeout=settings.grok_timeout_seconds,
                stream=True,
            )
        except Exception as exc:
            raise GrokClientError(f"Grok transport failed: {exc}", status_code=502) from exc

        if response.status_code != 200:
            body = response.content.decode("utf-8", "replace")[:400]
            logger.error("Grok upstream %s: %s", response.status_code, body)
            if _contains_cloudflare_challenge(response.status_code, body):
                raise GrokClientError(
                    "Grok Cloudflare challenge detected",
                    status_code=403,
                    body=body,
                    code="cloudflare_challenge",
                )
            raise GrokClientError(
                f"Grok upstream returned {response.status_code}",
                status_code=response.status_code,
                body=body,
                code="upstream_error",
            )

        try:
            async for raw_line in response.aiter_lines():
                event_type, data = classify_line(raw_line)
                if event_type == "done":
                    break
                if event_type != "data":
                    continue
                for delta in parse_text_delta(data):
                    yield delta
                    if delta.done:
                        return
        except Exception as exc:
            raise GrokClientError(f"Grok stream read failed: {exc}", status_code=502) from exc


async def complete_chat(settings: Settings, *, message: str, mode_id: str) -> str:
    chunks: list[str] = []
    async for delta in stream_chat(settings, message=message, mode_id=mode_id):
        if delta.done:
            break
        chunks.append(delta.content)
    return "".join(chunks)


async def smoke_skills(settings: Settings, *, extra_headers: dict[str, str] | None = None) -> dict:
    if not settings.grok_cookie:
        raise GrokClientError(
            "GROK_COOKIE is not configured",
            status_code=400,
            code="missing_grok_cookie",
        )

    payload = b'{"locale":"en"}'
    headers = _merge_safe_headers(_headers(settings), extra_headers)
    headers["Content-Type"] = "application/json"
    headers["Cookie"] = settings.grok_cookie
    headers["User-Agent"] = settings.grok_user_agent
    async with AsyncSession(**_session_kwargs(settings)) as session:
        try:
            response = await session.post(
                SKILLS_URL,
                headers=headers,
                data=payload,
                timeout=min(settings.grok_timeout_seconds, 30.0),
            )
        except Exception as exc:
            raise GrokClientError(f"Grok skills smoke failed: {exc}", status_code=502) from exc

    body = response.content.decode("utf-8", "replace")[:400]
    if response.status_code != 200:
        if _contains_cloudflare_challenge(response.status_code, body):
            raise GrokClientError(
                "Grok Cloudflare challenge detected",
                status_code=403,
                body=body,
                code="cloudflare_challenge",
            )
        raise GrokClientError(
            f"Grok skills smoke returned {response.status_code}",
            status_code=response.status_code,
            body=body,
            code="upstream_error",
        )
    return {"status_code": response.status_code, "body_preview": body[:120]}


# ---------------------------------------------------------------------------
# 图片生成（WebSocket）
# ---------------------------------------------------------------------------

_URL_PATTERN = re.compile(r"/images/([a-f0-9\-]+)\.(png|jpg|jpeg)", re.IGNORECASE)


def _parse_image_id_ext(url: str) -> tuple[str, str]:
    m = _URL_PATTERN.search(url or "")
    if m:
        return m.group(1), m.group(2).lower()
    return uuid.uuid4().hex, "jpg"


def _ws_headers(settings: Settings) -> dict[str, str]:
    headers = {
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Origin": "https://grok.com",
        "Pragma": "no-cache",
        "User-Agent": settings.grok_user_agent,
        "Cookie": settings.grok_cookie,
    }
    headers.update(_client_hints(settings.grok_browser, settings.grok_user_agent))
    return headers


def _build_reset_msg() -> dict[str, Any]:
    return {
        "type": "conversation.item.create",
        "timestamp": int(time.time() * 1000),
        "item": {"type": "message", "content": [{"type": "reset"}]},
    }


def _build_imagine_msg(
    request_id: str,
    prompt: str,
    aspect_ratio: str,
    enable_pro: bool,
    *,
    is_initial: bool = False,
    image_data: str | None = None,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if image_data and is_initial:
        content.append({"type": "input_image", "imageUrl": image_data})
    content.append({
        "requestId": request_id,
        "text": prompt,
        "type": "input_text",
        "properties": {
            "section_count": 0,
            "is_kids_mode": False,
            "enable_nsfw": True,
            "skip_upsampler": False,
            "enable_side_by_side": True,
            "is_initial": is_initial,
            "aspect_ratio": aspect_ratio,
            "enable_pro": enable_pro,
        },
    })
    return {
        "type": "conversation.item.create",
        "timestamp": int(time.time() * 1000),
        "item": {"type": "message", "content": content},
    }


@dataclass(slots=True)
class _ImgSlot:
    image_id: str
    order: int
    last_blob: str = field(default="")
    last_url: str = field(default="")
    done: bool = field(default=False)


@dataclass(frozen=True, slots=True)
class ImageResult:
    session_id: str
    filename: str   # "{image_id}.{ext}"
    order: int

    @property
    def serve_path(self) -> str:
        return f"{self.session_id}/{self.filename}"


def _init_session(session_id: str, *, prompt: str, source: str, aspect_ratio: str) -> Path:
    """创建 session 目录并写入 session.json 元数据。"""
    d = IMAGE_DIR / session_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "session.json").write_text(json.dumps({
        "session_id": session_id,
        "prompt": prompt,
        "source": source,
        "aspect_ratio": aspect_ratio,
        "created_at": time.time(),
    }, ensure_ascii=False), encoding="utf-8")
    return d


def _save_image(blob_b64: str, image_id: str, ext: str, *, session_dir: Path) -> str:
    filename = f"{image_id}.{ext}"
    (session_dir / filename).write_bytes(base64.b64decode(blob_b64))
    return filename


async def _ws_connect(session: aiohttp.ClientSession, timeout: float, proxy: str | None) -> aiohttp.ClientWebSocketResponse:
    ssl_ctx = False
    _retries = 3
    last_exc: Exception | None = None
    for attempt in range(_retries):
        try:
            return await session.ws_connect(
                WS_IMAGINE_URL,
                timeout=aiohttp.ClientTimeout(total=timeout),
                proxy=proxy if proxy and proxy.startswith("http") else None,
                heartbeat=20,
                ssl=ssl_ctx,
            )
        except Exception as exc:
            last_exc = exc
            if attempt < _retries - 1:
                logger.warning("imagine WS connect failed (attempt %d/%d): %s", attempt + 1, _retries, exc)
                await asyncio.sleep(1.0)
    raise GrokClientError(f"Imagine WebSocket connect failed: {last_exc}", status_code=502) from last_exc


async def _collect_batch(
    ws: aiohttp.ClientWebSocketResponse,
    deadline: float,
    *,
    batch_probe_s: float = 3.0,
    session_dir: Path,
) -> tuple[list[ImageResult], bool]:
    """
    从已发送请求的 WS 上收一批图片，返回 (results, ws_closed)。

    协议：
    - start_stage 帧 → 注册 slot
    - image 帧（中间预览）→ 缓存 blob/url，不保存
    - completed 帧 → 保存成品
    - 所有已知 slot 都 completed 后，再探测 batch_probe_s 秒看有没有新 start_stage
    - 若无新 start_stage → 本批结束
    """
    slots: dict[str, _ImgSlot] = {}
    results: list[ImageResult] = []
    all_done_since: float | None = None

    while True:
        now = asyncio.get_event_loop().time()
        if now >= deadline:
            break

        # 如果所有已知 slot 都完成，进入批次结束探测
        if slots and all(s.done for s in slots.values()):
            if all_done_since is None:
                all_done_since = now
            probe_remaining = batch_probe_s - (now - all_done_since)
            if probe_remaining <= 0:
                return results, False
            recv_timeout = min(probe_remaining, deadline - now)
        else:
            all_done_since = None
            recv_timeout = min(15.0, deadline - now)

        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=recv_timeout)
        except asyncio.TimeoutError:
            # 探测超时且所有 slot 完成 → 批次结束
            if slots and all(s.done for s in slots.values()):
                return results, False
            continue

        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
            return results, True

        if msg.type != aiohttp.WSMsgType.TEXT:
            continue

        try:
            data = json.loads(msg.data)
        except Exception:
            continue

        frame_type = data.get("type")

        if frame_type == "json":
            status = data.get("current_status")
            image_id = str(data.get("image_id") or data.get("job_id") or "")
            if not image_id:
                continue

            if status == "start_stage":
                all_done_since = None  # 新 slot 到来，重置探测
                slots[image_id] = _ImgSlot(
                    image_id=image_id,
                    order=int(data.get("order") or 0),
                )
                logger.info("imagine slot started: image_id=%s order=%s", image_id[:8], data.get("order"))

            elif status == "completed":
                slot = slots.get(image_id)
                if slot is None or slot.done:
                    continue
                slot.done = True
                if data.get("moderated"):
                    logger.warning("imagine slot moderated: image_id=%s", image_id[:8])
                    continue
                if not slot.last_blob:
                    logger.warning("imagine slot completed but no blob: image_id=%s", image_id[:8])
                    continue
                _, ext = _parse_image_id_ext(slot.last_url)
                filename = await asyncio.to_thread(_save_image, slot.last_blob, image_id, ext, session_dir=session_dir)
                results.append(ImageResult(
                    session_id=session_dir.name,
                    filename=filename,
                    order=slot.order,
                ))
                logger.info("imagine image saved: session=%s file=%s", session_dir.name, filename)

        elif frame_type == "image":
            # 中间预览帧：仅缓存 blob/url，不保存
            url = data.get("url", "")
            blob = data.get("blob", "")
            iid, _ = _parse_image_id_ext(url)
            slot = slots.get(iid)
            if slot and not slot.done and blob:
                slot.last_blob = blob
                slot.last_url = url

        elif frame_type == "error":
            err = data.get("err_msg") or str(data)
            raise GrokClientError(f"Imagine upstream error: {err}", status_code=502)

    return results, False


# generate_images 和 stream_imagine_batches 已迁移至 ws_gateway.WsGateway。
# 此处保留签名供向后兼容，但推荐直接使用 ws_gateway。

# ---------------------------------------------------------------------------
# 视频生成（REST）
# ---------------------------------------------------------------------------
# 视频生成：走 chat 接口 + SSE 流式解析，下载后本地缓存

VIDEO_DIR = IMAGE_DIR / "_videos"
_VIDEO_CREATE_URL = "https://grok.com/rest/media/post/create"
_VIDEO_ASSETS_BASE = "https://assets.grok.com"
_VIDEO_SSE_TIMEOUT = 300.0  # 视频生成最多等 5 分钟


def _video_headers(settings: Settings) -> dict[str, str]:
    h = _headers(settings)
    h["Referer"] = "https://grok.com/imagine"
    return h


def _build_video_sse_payload(
    prompt: str,
    parent_post_id: str,
    aspect_ratio: str,
    duration: int,
) -> dict[str, Any]:
    return {
        "temporary": True,
        "modelName": "imagine-video-gen",
        "message": prompt + " --mode=custom",
        "enableSideBySide": True,
        "responseMetadata": {
            "experiments": [],
            "modelConfigOverride": {
                "modelMap": {
                    "videoGenModelConfig": {
                        "parentPostId": parent_post_id,
                        "aspectRatio": aspect_ratio,
                        "videoLength": duration,
                        "resolutionName": "480p",
                    }
                }
            },
        },
    }


async def create_video(
    settings: Settings,
    *,
    prompt: str,
    aspect_ratio: str = "16:9",
    duration: int = 6,
    resolution: str = "480p",
    session_id: str | None = None,
    model_label: str = "grok-imagine-video",
) -> str:
    """三步：创建容器 → SSE 等待完成 → 下载到本地。

    session_id 非空时，把视频保存到 IMAGE_DIR/<session_id>/<post_id>.mp4 并写入
    session.json（与图片 session 一致的结构），让图库可按 session 显示 prompt。"""
    if not settings.grok_cookie:
        raise GrokClientError("GROK_COOKIE is not configured", status_code=400, code="missing_grok_cookie")

    # Step 1: 创建视频容器，必须带 prompt（用 curl_cffi 绕过 Cloudflare TLS 检测）
    create_payload = json.dumps(
        {"mediaType": "MEDIA_POST_TYPE_VIDEO", "prompt": prompt},
        ensure_ascii=False,
    ).encode("utf-8")
    async with AsyncSession(**_session_kwargs(settings)) as create_sess:
        try:
            create_resp = await create_sess.post(
                _VIDEO_CREATE_URL,
                headers=_video_headers(settings),
                data=create_payload,
                timeout=30.0,
            )
            body = create_resp.content.decode("utf-8", "replace")
            if create_resp.status_code != 200:
                logger.error("video create error: status=%s body=%s", create_resp.status_code, body[:500])
                if _contains_cloudflare_challenge(create_resp.status_code, body):
                    raise GrokClientError("Cloudflare challenge detected", status_code=403, code="cloudflare_challenge")
                raise GrokClientError(
                    f"Video create returned {create_resp.status_code}",
                    status_code=create_resp.status_code, body=body[:300], code="upstream_error",
                )
            try:
                post_data = json.loads(body)["post"]
                post_id: str = post_data["id"]
                user_id: str = post_data["userId"]
            except (KeyError, ValueError) as exc:
                raise GrokClientError(f"Video create parse failed: {exc} body={body[:200]}", status_code=502) from exc
        except GrokClientError:
            raise
        except Exception as exc:
            raise GrokClientError(f"Video create transport failed: {exc}", status_code=502) from exc

    logger.info("video container created: post_id=%s user_id=%s prompt=%r", post_id, user_id, prompt[:40])

    # Step 2: SSE 流等待生成完成（progress → 100）
    sse_payload = json.dumps(
        _build_video_sse_payload(prompt, post_id, aspect_ratio, duration),
        ensure_ascii=False,
    ).encode("utf-8")

    async with AsyncSession(**_session_kwargs(settings)) as sse_sess:
        try:
            sse_resp = await sse_sess.post(
                CHAT_URL,
                headers=_video_headers(settings),
                data=sse_payload,
                timeout=_VIDEO_SSE_TIMEOUT,
                stream=True,
            )
        except Exception as exc:
            raise GrokClientError(f"Video SSE transport failed: {exc}", status_code=502) from exc

        if sse_resp.status_code != 200:
            body = sse_resp.content.decode("utf-8", "replace")[:400]
            if _contains_cloudflare_challenge(sse_resp.status_code, body):
                raise GrokClientError("Cloudflare challenge on SSE", status_code=403, code="cloudflare_challenge")
            raise GrokClientError(
                f"Video SSE returned {sse_resp.status_code}",
                status_code=sse_resp.status_code, body=body, code="upstream_error",
            )

        try:
            async for raw_line in sse_resp.aiter_lines():
                event_type, data = classify_line(raw_line)
                if event_type == "done":
                    break
                if event_type != "data":
                    continue
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                svgr = (
                    (obj.get("result") or {})
                    .get("response", {})
                    .get("streamingVideoGenerationResponse")
                )
                if not svgr:
                    continue
                progress = svgr.get("progress", 0)
                moderated = svgr.get("moderated", False)
                logger.info("video progress=%s%%", progress)
                if moderated:
                    raise GrokClientError("Video blocked by moderation", status_code=400, code="moderated")
                if progress >= 100:
                    break  # 生成完成，准备下载
        except GrokClientError:
            raise
        except Exception as exc:
            raise GrokClientError(f"Video SSE read failed: {exc}", status_code=502) from exc

    logger.info("video SSE complete, downloading: post_id=%s user_id=%s", post_id, user_id)

    # Step 3: 从 assets.grok.com 下载视频
    if session_id:
        sess_dir = IMAGE_DIR / session_id
        sess_dir.mkdir(parents=True, exist_ok=True)
        filepath = sess_dir / f"{post_id}.mp4"
        # 写 session.json，让图库按 session 分类显示 prompt
        meta = sess_dir / "session.json"
        if not meta.exists():
            meta.write_text(json.dumps({
                "session_id": session_id,
                "prompt": prompt,
                "source": "video",
                "aspect_ratio": aspect_ratio,
                "model": model_label,
                "created_at": time.time(),
            }, ensure_ascii=False), encoding="utf-8")
    else:
        VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        filepath = VIDEO_DIR / f"{post_id}.mp4"
    video_asset_url = f"{_VIDEO_ASSETS_BASE}/users/{user_id}/generated/{post_id}/generated_video.mp4?cache=1"

    dl_headers = _video_headers(settings)
    dl_headers["Range"] = "bytes=0-"

    connector2 = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector2) as dl:
        try:
            async with dl.get(
                video_asset_url,
                headers=dl_headers,
                timeout=aiohttp.ClientTimeout(total=120.0),
            ) as dl_resp:
                content = await dl_resp.read()
                if dl_resp.status in (200, 206) and len(content) > 4096:
                    filepath.write_bytes(content)
                    logger.info("video saved: %s (%d bytes)", filepath.name, len(content))
                    return post_id
                raise GrokClientError(
                    f"Video download failed: status={dl_resp.status} size={len(content)}",
                    status_code=502, code="video_download_failed",
                )
        except GrokClientError:
            raise
        except Exception as exc:
            raise GrokClientError(f"Video download transport failed: {exc}", status_code=502) from exc


async def get_video_link(settings: Settings, video_id: str) -> str | None:
    """返回本地已缓存视频的 serve URL；视频在 create_video 时已下载。

    优先查 IMAGE_DIR/*/<video_id>.<ext>（按 session 归类的新视频）；
    回退到 VIDEO_DIR/_videos（老路径，兼容已存在的视频）。"""
    for ext in ("mp4", "webm", "mov"):
        # 新路径：按 session 归类
        if IMAGE_DIR.exists():
            for sess_dir in IMAGE_DIR.iterdir():
                if not sess_dir.is_dir() or sess_dir.name == "_videos":
                    continue
                candidate = sess_dir / f"{video_id}.{ext}"
                if candidate.exists():
                    return f"/v1/files/video/{sess_dir.name}/{video_id}.{ext}"
        # 兼容旧路径
        if (VIDEO_DIR / f"{video_id}.{ext}").exists():
            return f"/v1/files/video/_videos/{video_id}.{ext}"
    url_file = VIDEO_DIR / f"{video_id}.url"
    if url_file.exists():
        return url_file.read_text(encoding="utf-8").strip()
    return None


# ── Grok Files ──────────────────────────────────────────────────────────────

_GROK_ASSETS_URL = "https://grok.com/rest/assets"
_GROK_DELETE_URL = "https://grok.com/rest/media/post/delete"
_GROK_ASSETS_DL_BASE = "https://assets.grok.com"
GROK_FILES_DIR = Path("data/grok-files")


def _files_headers(settings: Settings) -> dict[str, str]:
    h = _headers(settings)
    h["Referer"] = "https://grok.com/files"
    h.pop("Content-Type", None)
    return h


async def list_grok_assets(
    settings: Settings,
    *,
    page_token: str = "",
    page_size: int = 50,
) -> dict:
    if not settings.grok_cookie:
        raise GrokClientError("GROK_COOKIE is not configured", status_code=400, code="missing_grok_cookie")
    params: dict[str, str] = {
        "pageSize": str(page_size),
        "orderBy": "ORDER_BY_LAST_USE_TIME",
        "source": "SOURCE_ANY",
        "isLatest": "true",
        "includeImagineFiles": "true",
    }
    if page_token:
        params["pageToken"] = page_token
    async with AsyncSession(**_session_kwargs(settings)) as sess:
        try:
            resp = await sess.get(
                _GROK_ASSETS_URL,
                headers=_files_headers(settings),
                params=params,
                timeout=30.0,
            )
            body = resp.content.decode("utf-8", "replace")
            if resp.status_code != 200:
                if _contains_cloudflare_challenge(resp.status_code, body):
                    raise GrokClientError("Cloudflare challenge", status_code=403, code="cloudflare_challenge")
                raise GrokClientError(f"Assets list returned {resp.status_code}", status_code=resp.status_code, body=body[:300])
            return json.loads(body)
        except GrokClientError:
            raise
        except Exception as exc:
            raise GrokClientError(f"Assets list failed: {exc}", status_code=502) from exc


async def delete_grok_asset(settings: Settings, asset_id: str) -> None:
    if not settings.grok_cookie:
        raise GrokClientError("GROK_COOKIE is not configured", status_code=400, code="missing_grok_cookie")
    payload = json.dumps({"id": asset_id}).encode("utf-8")
    h = _files_headers(settings)
    h["Content-Type"] = "application/json"
    async with AsyncSession(**_session_kwargs(settings)) as sess:
        try:
            resp = await sess.post(_GROK_DELETE_URL, headers=h, data=payload, timeout=30.0)
            body = resp.content.decode("utf-8", "replace")
            if resp.status_code != 200:
                if _contains_cloudflare_challenge(resp.status_code, body):
                    raise GrokClientError("Cloudflare challenge", status_code=403, code="cloudflare_challenge")
                raise GrokClientError(f"Delete returned {resp.status_code}", status_code=resp.status_code, body=body[:200])
        except GrokClientError:
            raise
        except Exception as exc:
            raise GrokClientError(f"Delete asset failed: {exc}", status_code=502) from exc


async def stream_grok_asset(settings: Settings, key: str):
    """从 assets.grok.com 流式下载文件，返回 (content_type, async_generator)。"""
    if not settings.grok_cookie:
        raise GrokClientError("GROK_COOKIE is not configured", status_code=400, code="missing_grok_cookie")
    url = f"{_GROK_ASSETS_DL_BASE}/{key}"
    dl_headers = _headers(settings)
    dl_headers["Referer"] = "https://grok.com/"
    connector = aiohttp.TCPConnector(ssl=False)
    session = aiohttp.ClientSession(connector=connector)
    try:
        resp = await session.get(url, headers=dl_headers, timeout=aiohttp.ClientTimeout(total=120.0))
        if resp.status not in (200, 206):
            await session.close()
            body = await resp.text()
            raise GrokClientError(f"Asset download returned {resp.status}", status_code=resp.status, body=body[:200])
        content_type = resp.headers.get("Content-Type", "application/octet-stream")

        async def _gen():
            try:
                async for chunk in resp.content.iter_chunked(65536):
                    yield chunk
            finally:
                await session.close()

        return content_type, _gen()
    except GrokClientError:
        await session.close()
        raise
    except Exception as exc:
        await session.close()
        raise GrokClientError(f"Asset download failed: {exc}", status_code=502) from exc


# ---------------------------------------------------------------------------
# FlareSolverr：刷新 cf_clearance / __cf_bm，UA 与 curl_cffi chrome142 匹配
# ---------------------------------------------------------------------------

_FLARESOLVERR_TIMEOUT = 120.0           # FlareSolverr API 调用超时
_FLARESOLVERR_CHALLENGE_TIMEOUT = 90000  # CF 挑战求解最大耗时（毫秒）


_FLARESOLVERR_SESSION_ID = "grok"  # 全局复用同一个 FlareSolverr session


def _build_flaresolverr_proxy(grok_proxy: str, http_bridge_url: str = "") -> dict[str, str] | None:
    """构造 FlareSolverr 的 proxy 对象。

    - http_bridge_url 非空：用 HTTP 代理（已含认证）。这是同机部署时的方案，
      避免 Chrome 在容器内 SOCKS5+auth 失败。
    - http_bridge_url 为空：返回 None，让 FlareSolverr 用自己的网络路由
      （远程部署场景：FlareSolverr 实例与 SOCKS5 服务在同一机器或同一 VPN，
      出口 IP 与 xgate 直连 SOCKS5 一致，无需重复走代理）。
    """
    if http_bridge_url:
        return {"url": http_bridge_url.strip()}
    return None


async def _flaresolverr_call(
    base: str,
    payload: dict[str, Any],
    *,
    timeout: float = _FLARESOLVERR_TIMEOUT,
) -> dict[str, Any]:
    async with aiohttp.ClientSession() as http:
        try:
            async with http.post(
                f"{base}/v1",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                return await resp.json(content_type=None)
        except Exception as exc:
            raise GrokClientError(
                f"FlareSolverr request failed: {exc}",
                status_code=502,
                code="flaresolverr_unreachable",
            ) from exc


async def _flaresolverr_ensure_session(
    base: str,
    proxy: dict[str, str] | None,
    *,
    recreate: bool = False,
) -> None:
    """确保目标 session 存在；不存在则用 proxy 创建。
    recreate=True 时先销毁再建，确保浏览器 cookie jar 干净（每次都解新 CF 挑战）。"""
    if recreate:
        try:
            await _flaresolverr_call(
                base,
                {"cmd": "sessions.destroy", "session": _FLARESOLVERR_SESSION_ID},
                timeout=15.0,
            )
        except Exception:
            pass
    else:
        list_resp = await _flaresolverr_call(base, {"cmd": "sessions.list"}, timeout=10.0)
        if _FLARESOLVERR_SESSION_ID in (list_resp.get("sessions") or []):
            return
    create_payload: dict[str, Any] = {
        "cmd": "sessions.create",
        "session": _FLARESOLVERR_SESSION_ID,
    }
    if proxy:
        create_payload["proxy"] = proxy
    create_resp = await _flaresolverr_call(base, create_payload, timeout=60.0)
    if create_resp.get("status") != "ok":
        raise GrokClientError(
            f"FlareSolverr sessions.create failed: {create_resp.get('message')}",
            status_code=502,
            code="flaresolverr_session_error",
            body=str(create_resp)[:300],
        )
    logger.info("FlareSolverr session created (proxy=%s)", "yes" if proxy else "no")


async def flaresolverr_refresh_cf(
    flaresolverr_url: str,
    *,
    target_url: str = "https://grok.com/",
    existing_cookies: dict[str, str] | None = None,
    grok_proxy: str = "",
    flaresolverr_proxy_url: str = "",
) -> tuple[dict[str, str], str]:
    """
    通过 FlareSolverr 刷新 grok.com 的 CF cookies（cf_clearance / __cf_bm）。

    flaresolverr_proxy_url：HTTP 代理 URL（如 proxy-bridge 容器提供的
    http://proxy-bridge:8118），优先用此作为 FlareSolverr session 的代理；
    否则回退到 grok_proxy（直接 SOCKS5）。
    返回 ({cookie_name: value}, user_agent)。
    """
    base = flaresolverr_url.rstrip("/")
    proxy = _build_flaresolverr_proxy(grok_proxy, flaresolverr_proxy_url)

    # 每次都销毁重建 session，让 FlareSolverr 在干净浏览器（无 cf_clearance）里重新解挑战。
    # 不销毁的话：旧 cf_clearance 还在 jar 里，CF 不再发起挑战，FlareSolverr 返回的还是旧值。
    await _flaresolverr_ensure_session(base, proxy, recreate=True)

    payload: dict[str, Any] = {
        "cmd": "request.get",
        "url": target_url,
        "session": _FLARESOLVERR_SESSION_ID,
        "maxTimeout": _FLARESOLVERR_CHALLENGE_TIMEOUT,
        "returnOnlyCookies": True,
    }
    if existing_cookies:
        payload["cookies"] = [
            {"name": name, "value": value}
            for name, value in existing_cookies.items()
            if value
        ]

    data = await _flaresolverr_call(base, payload)

    if data.get("status") != "ok":
        raise GrokClientError(
            f"FlareSolverr error: {data.get('message', 'unknown')}",
            status_code=502,
            code="flaresolverr_error",
            body=str(data)[:300],
        )

    solution = data.get("solution") or {}
    raw_cookies = solution.get("cookies") or []
    user_agent = (solution.get("userAgent") or "").strip()

    cookies: dict[str, str] = {
        c["name"]: c["value"]
        for c in raw_cookies
        if c.get("name") and c.get("value")
    }
    return cookies, user_agent


async def flaresolverr_destroy_session(flaresolverr_url: str) -> None:
    """关停时调用，释放 FlareSolverr 持有的 Chrome 实例。"""
    base = flaresolverr_url.rstrip("/")
    try:
        await _flaresolverr_call(
            base,
            {"cmd": "sessions.destroy", "session": _FLARESOLVERR_SESSION_ID},
            timeout=15.0,
        )
    except Exception as exc:
        logger.debug("FlareSolverr destroy session failed: %s", exc)


def merge_grok_cookies(
    base_cookie_str: str,
    fresh_cookies: dict[str, str],
) -> str:
    """
    把 fresh_cookies（来自 FlareSolverr）合并进 base_cookie_str（已有 cookies）。

    fresh_cookies 优先级更高，覆盖同名 cookie。常用于把新解出的 cf_clearance / __cf_bm
    合并到现有的 sso/sso-rw 登录态。
    """
    merged: dict[str, str] = {}
    if base_cookie_str:
        for part in base_cookie_str.split(";"):
            seg = part.strip()
            if not seg or "=" not in seg:
                continue
            name, _, value = seg.partition("=")
            merged[name.strip()] = value.strip()
    merged.update({k: v for k, v in fresh_cookies.items() if v})

    priority = ["i18nextLng", "sso-rw", "sso", "x-userid", "cf_clearance", "__cf_bm"]
    parts: list[str] = []
    for name in priority:
        if name in merged:
            parts.append(f"{name}={merged[name]}")
    for name, value in merged.items():
        if name not in priority:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def parse_cookie_string(cookie_str: str) -> dict[str, str]:
    """解析 'k1=v1; k2=v2' 形式为 dict，便于传给 FlareSolverr。"""
    result: dict[str, str] = {}
    if not cookie_str:
        return result
    for part in cookie_str.split(";"):
        seg = part.strip()
        if not seg or "=" not in seg:
            continue
        name, _, value = seg.partition("=")
        result[name.strip()] = value.strip()
    return result


async def save_grok_asset_local(settings: Settings, key: str, filename: str) -> str:
    """从 assets.grok.com 下载文件并保存到 data/grok-files/，返回保存路径字符串。"""
    if not settings.grok_cookie:
        raise GrokClientError("GROK_COOKIE is not configured", status_code=400, code="missing_grok_cookie")
    GROK_FILES_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[^\w.\-]', '_', filename)[:200] or "file"
    dest = GROK_FILES_DIR / safe_name
    # 防止重名覆盖
    stem = dest.stem
    suffix = dest.suffix
    counter = 1
    while dest.exists():
        dest = GROK_FILES_DIR / f"{stem}_{counter}{suffix}"
        counter += 1

    url = f"{_GROK_ASSETS_DL_BASE}/{key}"
    dl_headers = _headers(settings)
    dl_headers["Referer"] = "https://grok.com/"
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as sess:
        try:
            async with sess.get(url, headers=dl_headers, timeout=aiohttp.ClientTimeout(total=120.0)) as resp:
                if resp.status not in (200, 206):
                    body = await resp.text()
                    raise GrokClientError(
                        f"Asset download returned {resp.status}", status_code=resp.status, body=body[:200]
                    )
                data = await resp.read()
        except GrokClientError:
            raise
        except Exception as exc:
            raise GrokClientError(f"Asset download failed: {exc}", status_code=502) from exc

    if len(data) == 0:
        raise GrokClientError("Asset download returned empty file", status_code=502, code="empty_file")
    dest.write_bytes(data)
    logger.info("grok file saved: %s (%d bytes)", dest, len(data))
    return str(dest)
