"""Grok Web 文本聊天客户端。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Union
from urllib.parse import urlparse

import aiohttp
from curl_cffi.requests import AsyncSession

from .config import Settings

logger = logging.getLogger(__name__)

CHAT_URL = "https://grok.com/rest/app-chat/conversations/new"
_RATE_LIMITS_URL = "https://grok.com/rest/rate-limits"
# TODO: x-statsig-id / baggage 当前硬编码，理论上有被指纹识别风险
#       Grok web 端每个请求生成不同值。后续重构成动态生成。
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
    content: str = ""
    done: bool = False
    image_urls: tuple[str, ...] = ()  # LLM 自主生成的图片 URL（assets.grok.com/...）
    placeholder_card: bool = False     # cardAttachment 占位（image_chunk=null），LLM 已下发生图意图


# ---------------------------------------------------------------------------
# MCP 路径：结构化事件类型（parse_event / stream_chat_events 使用）
# 现有 GrokTextDelta / parse_text_delta / stream_chat 完全不变，OpenAI 路径继续使用。
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class GrokConversationStarted:
    conversation_id: str
    title: str | None = None


@dataclass(frozen=True, slots=True)
class GrokReasoningHeader:
    step_id: int
    rollout: str    # "Grok" | "Agent 1" | "Agent 2" | "Agent 3"
    label: str      # e.g. "Searching recent posts"


@dataclass(frozen=True, slots=True)
class GrokReasoningToken:
    rollout: str
    token: str


@dataclass(frozen=True, slots=True)
class GrokToolCall:
    card_id: str
    tool: str       # "xSearch" | "webSearch" | "chatroomSend"
    args: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GrokXSearchResults:
    results: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class GrokWebSearchResults:
    results: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class GrokCitation:
    card_id: str
    url: str


@dataclass(frozen=True, slots=True)
class GrokFinalToken:
    token: str


@dataclass(frozen=True, slots=True)
class GrokImageEvent:
    image_urls: tuple[str, ...] = ()
    placeholder_card: bool = False


@dataclass(frozen=True, slots=True)
class GrokDone:
    response_id: str
    title: str | None = None
    follow_up_suggestions: tuple[str, ...] = ()


GrokEvent = Union[
    GrokConversationStarted, GrokReasoningHeader, GrokReasoningToken,
    GrokToolCall, GrokXSearchResults, GrokWebSearchResults, GrokCitation,
    GrokFinalToken, GrokImageEvent, GrokDone,
]

_KNOWN_TOOLS = ("xSearch", "webSearch", "chatroomSend")


def parse_event(data: str) -> list[GrokEvent]:
    """将单条 SSE data 字符串解析为 GrokEvent 列表。无网络调用，纯本地解析。"""
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return []

    result = obj.get("result") or {}
    events: list[GrokEvent] = []

    # 会话创建（首条 SSE）
    conv = result.get("conversation")
    if isinstance(conv, dict):
        conv_id = conv.get("conversationId", "")
        if conv_id:
            events.append(GrokConversationStarted(
                conversation_id=conv_id,
                title=conv.get("title") or None,
            ))
        return events

    response = result.get("response") if isinstance(result.get("response"), dict) else result
    if not isinstance(response, dict):
        return events

    # 工具调用卡片（toolUsageCard）
    tuc = response.get("toolUsageCard")
    if isinstance(tuc, dict):
        card_id = tuc.get("toolUsageCardId", "")
        for tool_name in _KNOWN_TOOLS:
            tool_data = tuc.get(tool_name)
            if isinstance(tool_data, dict):
                events.append(GrokToolCall(
                    card_id=card_id,
                    tool=tool_name,
                    args=tool_data.get("args") or {},
                ))
                break

    # X 搜索结果
    xsr = response.get("xSearchResults")
    if isinstance(xsr, dict):
        results = xsr.get("results")
        if isinstance(results, list):
            events.append(GrokXSearchResults(results=results))

    # Web 搜索结果（仅非空时才发）
    wsr = response.get("webSearchResults")
    if isinstance(wsr, dict):
        results = wsr.get("results")
        if isinstance(results, list) and results:
            events.append(GrokWebSearchResults(results=results))

    # cardAttachment：citation_card 或 generated_image_card
    card = response.get("cardAttachment")
    if isinstance(card, dict):
        raw = card.get("jsonData")
        if isinstance(raw, str) and raw:
            try:
                inner = json.loads(raw)
                card_type = inner.get("cardType", "")
                if card_type == "citation_card":
                    url = inner.get("url", "")
                    if url:
                        events.append(GrokCitation(card_id=inner.get("id", ""), url=url))
                elif card_type == "generated_image_card":
                    ic = inner.get("image_chunk")
                    if isinstance(ic, dict):
                        img_url = ic.get("imageUrl", "")
                        if img_url and ic.get("progress") == 100 and not ic.get("moderated"):
                            full = img_url if img_url.startswith("http") else f"https://assets.grok.com/{img_url.lstrip('/')}"
                            events.append(GrokImageEvent(image_urls=(full,)))
                    else:
                        events.append(GrokImageEvent(placeholder_card=True))
            except Exception:
                pass

    # Reasoning token（isThinking=True）
    if response.get("isThinking") is True:
        rollout = response.get("rolloutId", "")
        token = response.get("token", "")
        step_id = response.get("messageStepId")
        if response.get("messageTag") == "header" and token:
            events.append(GrokReasoningHeader(
                step_id=int(step_id) if step_id is not None else 0,
                rollout=rollout,
                label=token,
            ))
        elif token:
            events.append(GrokReasoningToken(rollout=rollout, token=token))
        return events

    # finalMetadata：优先级高于 isSoftStop，包含 followUpSuggestions
    fm = response.get("finalMetadata")
    if isinstance(fm, dict):
        response_id = response.get("responseId", "")
        suggestions = fm.get("followUpSuggestions") or []
        follow_ups = tuple(
            s["label"] for s in suggestions
            if isinstance(s, dict) and s.get("label")
        )
        events.append(GrokDone(response_id=response_id, follow_up_suggestions=follow_ups))
        return events

    # isSoftStop：流结束信号，无 followUpSuggestions
    if response.get("isSoftStop"):
        response_id = response.get("responseId", "")
        events.append(GrokDone(response_id=response_id))
        return events

    # Final token（正文输出）
    if response.get("messageTag") == "final" and not response.get("isThinking"):
        token = response.get("token")
        if token is not None:
            events.append(GrokFinalToken(token=str(token)))

    return events


_CHAT_CONTINUE_URL = "https://grok.com/rest/app-chat/conversations/{conversation_id}/responses"


async def stream_chat_events(
    settings: Settings,
    *,
    message: str,
    mode_id: str,
    conversation_id: str | None = None,
    parent_response_id: str | None = None,
    disable_search: bool = False,
    temporary: bool = True,
) -> AsyncGenerator[GrokEvent, None]:
    """按 GrokEvent 联合类型流式产出结构化事件。

    conversation_id 非空 → 续轮（POST /conversations/{id}/responses + parentResponseId）。
    conversation_id 为 None → 新建会话（POST /conversations/new）。
    与旧 stream_chat 完全独立，互不影响。
    """
    if not settings.grok_cookie:
        raise GrokClientError(
            "GROK_COOKIE is not configured",
            status_code=400,
            code="missing_grok_cookie",
        )

    if conversation_id:
        url = _CHAT_CONTINUE_URL.format(conversation_id=conversation_id)
        payload: dict[str, Any] = {
            "message": message,
            "fileAttachments": [],
            "imageAttachments": [],
            "disableSearch": disable_search,
            "enableImageGeneration": True,
            "returnImageBytes": False,
            "returnRawGrokInXaiRequest": False,
            "enableImageStreaming": True,
            "imageGenerationCount": 2,
            "forceConcise": False,
            "enableSideBySide": True,
            "sendFinalMetadata": True,
            "disableTextFollowUps": False,
            "isAsyncChat": False,
            "disableSelfHarmShortCircuit": False,
            "metadata": {},
            "modeId": mode_id or "fast",
        }
        if parent_response_id:
            payload["parentResponseId"] = parent_response_id
    else:
        url = CHAT_URL
        payload = build_chat_payload(message, mode_id, temporary=temporary)
        if disable_search:
            payload["disableSearch"] = True

    raw_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    async with AsyncSession(**_session_kwargs(settings)) as session:
        try:
            response = await session.post(
                url,
                headers=_headers(settings),
                data=raw_payload,
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
                for event in parse_event(data):
                    yield event
        except Exception as exc:
            raise GrokClientError(f"Grok stream read failed: {exc}", status_code=502) from exc


def build_chat_payload(
    message: str, mode_id: str,
    *, image_count: int = 2, file_attachments: list[str] | None = None,
    temporary: bool = True,
) -> dict[str, Any]:
    """对齐 grok 网页端 POST /rest/app-chat/conversations/new 真实 payload。

    重要：抓包对比同一用户开启"私密模式"前后，差异**只**在 `temporary: true`。
    用 temporary=true 时该会话不写入用户历史；这也是绕过历史画像化记忆审核
    的关键开关，对敏感 prompt 通过率明显更高。

    关键字段：
    - temporary=True：私密模式（默认开启）
    - modeId="fast"：标准 chat + 工具调用模式
    - responseMetadata={}：注意是 responseMetadata（首条消息形态），
      不是 metadata。/conversations/{id}/responses 后续消息才用 metadata。
    """
    return {
        "temporary": bool(temporary),
        "message": message,
        "fileAttachments": list(file_attachments or []),
        "imageAttachments": [],
        "disableSearch": False,
        "enableImageGeneration": True,
        "returnImageBytes": False,
        "returnRawGrokInXaiRequest": False,
        "enableImageStreaming": True,
        "imageGenerationCount": max(1, int(image_count or 2)),
        "forceConcise": False,
        "enableSideBySide": True,
        "sendFinalMetadata": True,
        "disableTextFollowUps": False,
        "responseMetadata": {},
        "disableMemory": False,
        "forceSideBySide": False,
        "isAsyncChat": False,
        "disableSelfHarmShortCircuit": False,
        "deviceEnvInfo": {
            "darkModeEnabled": False,
            "devicePixelRatio": 2,
            "screenWidth": 1800,
            "screenHeight": 1169,
            "viewportWidth": 1055,
            "viewportHeight": 976,
        },
        "modeId": mode_id or "fast",
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
    # chat SSE 顶层是 result.{xxx} 不是 result.response.{xxx}
    result = obj.get("result") or {}
    response = result.get("response") if isinstance(result.get("response"), dict) else result
    if not isinstance(response, dict):
        return []
    deltas: list[GrokTextDelta] = []

    # cardAttachment 是 grok chat 出图的载体。两个阶段：
    #   阶段 1：占位 — jsonData.image_chunk == null，仅含 prompt（生图请求已下发）
    #   阶段 2：增量 — jsonData.image_chunk.{imageUrl, progress, moderated, imageUuid}
    #            progress=50 是 part-0 渐进式预览，progress=100 才是最终图
    # 真实 URL：image_chunk.imageUrl 形如 users/{uid}/generated/{uuid}/image.jpg
    # 拼 https://assets.grok.com/{imageUrl} 即可下载
    card = response.get("cardAttachment")
    if isinstance(card, dict):
        raw = card.get("jsonData")
        if isinstance(raw, str) and raw:
            try:
                inner = json.loads(raw)
                if inner.get("cardType") == "generated_image_card":
                    ic = inner.get("image_chunk")
                    if isinstance(ic, dict):
                        url = ic.get("imageUrl") or ""
                        progress = ic.get("progress")
                        moderated = bool(ic.get("moderated"))
                        # 只取最终图：progress=100 且未被审；忽略 part-N 的中间帧
                        if url and progress == 100 and not moderated:
                            full = url if url.startswith("http") else f"https://assets.grok.com/{url.lstrip('/')}"
                            deltas.append(GrokTextDelta(image_urls=(full,)))
                    else:
                        # image_chunk 为 null 说明这是占位（LLM 已下发生图意图）
                        deltas.append(GrokTextDelta(placeholder_card=True))
            except Exception:
                pass

    # 2. modelResponse / userResponse 中可能直接含 generatedImageUrls
    for key in ("modelResponse", "userResponse"):
        msg = response.get(key)
        if isinstance(msg, dict):
            urls = msg.get("generatedImageUrls") or []
            if urls:
                deltas.append(GrokTextDelta(image_urls=tuple(str(u) for u in urls if u)))

    if response.get("isSoftStop") or response.get("finalMetadata"):
        deltas.append(GrokTextDelta(done=True))
        return deltas
    token = response.get("token")
    if token is None or response.get("isThinking") is True:
        return deltas
    if response.get("messageTag") != "final":
        return deltas
    deltas.append(GrokTextDelta(content=str(token)))
    return deltas


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


async def chat_imagine(
    settings: Settings,
    *,
    message: str,
    mode_id: str = "fast",
    image_count: int = 2,
    aspect_ratio: str = "1:1",
) -> tuple[list[str], str]:
    """通过 chat 端口请求 LLM 生图（LLM 会自动 rephrase prompt）。

    image_count 与 aspect_ratio 都通过 **prompt 前缀注入** 控制（grok 网页端就是这么做的：
    用户在 message 里直接写"我要5张 portrait\\n\\n..."），payload 字段只是兜底。

    返回 (image_urls, model_text_summary)。image_urls 是 assets.grok.com 路径或绝对 URL。
    """
    if not settings.grok_cookie:
        raise GrokClientError("GROK_COOKIE is not configured", status_code=400, code="missing_grok_cookie")

    n = max(1, int(image_count or 2))
    # 抓包确认 cardAttachment.jsonData.orientation 字段取值是 portrait/landscape/square
    ratio = (aspect_ratio or "1:1").strip()
    if ratio in ("1:1",):
        orientation = "square"
    elif ratio in ("16:9", "3:2"):
        orientation = "landscape"
    else:  # 9:16 / 2:3 等
        orientation = "portrait"
    # grok 网页端靠 prompt 头部数量声明控制张数；imageGenerationCount 只是兜底字段，
    # 抓包显示用户写"我要5张"配 imageGenerationCount=2 时 LLM 实际生成了 5 张。
    # 因此 prompt 注入的数量为准，不在前端硬性限制。
    # 显式声明数量 + 宽高比 + 方向，让 LLM 三个信号都接收到
    message = (
        f"we need {n} images with aspect ratio {ratio} ({orientation} orientation)\n\n"
        f"{message}"
    )

    payload = json.dumps(
        # imageGenerationCount 字段保留 ≤ 4 兜底以匹配 grok 网页端行为
        build_chat_payload(message, mode_id, image_count=min(n, 4)),
        ensure_ascii=False,
    ).encode("utf-8")
    image_urls: list[str] = []
    text_chunks: list[str] = []
    placeholder_count = 0  # cardAttachment(image_chunk=null) 的占位数 — LLM 已下发生图意图
    raw_lines: list[str] = []  # 完整原始 SSE，失败时落盘
    async with AsyncSession(**_session_kwargs(settings)) as session:
        try:
            response = await session.post(
                CHAT_URL, headers=_headers(settings), data=payload,
                timeout=settings.grok_timeout_seconds, stream=True,
            )
        except Exception as exc:
            raise GrokClientError(f"chat transport failed: {exc}", status_code=502) from exc
        if response.status_code != 200:
            body = response.content.decode("utf-8", "replace")[:400]
            if _contains_cloudflare_challenge(response.status_code, body):
                raise GrokClientError("Cloudflare challenge", status_code=403, code="cloudflare_challenge")
            raise GrokClientError(
                f"chat returned {response.status_code}", status_code=response.status_code,
                body=body, code="upstream_error",
            )
        try:
            async for raw_line in response.aiter_lines():
                # 原始行存入 raw_lines，失败时落盘备查
                try:
                    line_str = (raw_line.decode("utf-8", "replace")
                                if isinstance(raw_line, (bytes, bytearray)) else str(raw_line))
                    raw_lines.append(line_str)
                except Exception:
                    pass
                event_type, data = classify_line(raw_line)
                if event_type == "done":
                    break
                if event_type != "data":
                    continue
                for delta in parse_text_delta(data):
                    if delta.placeholder_card:
                        placeholder_count += 1
                    if delta.image_urls:
                        for u in delta.image_urls:
                            if not u:
                                continue
                            # `prompt://...` 是 LLM 改写后的 prompt 透传伪 URL，不是真实图片
                            if u.startswith("prompt://"):
                                logger.debug("chat_imagine: rephrased prompt=%s", u[9:80])
                                continue
                            if u not in image_urls:
                                image_urls.append(u)
                                logger.info("chat_imagine: got image url=%s", u[:120])
                    if delta.content:
                        text_chunks.append(delta.content)
                    if delta.done and image_urls:
                        # 拿到图就够了；若 LLM 还在絮叨可继续，但最简实现先 break
                        return image_urls, "".join(text_chunks).strip()
        except Exception as exc:
            raise GrokClientError(f"chat stream read failed: {exc}", status_code=502) from exc
    if not image_urls:
        # 落盘原始 SSE 全文，方便复盘"为什么没拿到图"
        try:
            from pathlib import Path as _Path
            import time as _t
            dbg_dir = _Path("data/file")
            dbg_dir.mkdir(parents=True, exist_ok=True)
            ts = int(_t.time() * 1000)
            dbg_path = dbg_dir / f"chat-debug-{ts}.log"
            with open(dbg_path, "w", encoding="utf-8") as f:
                f.write(f"# chat_imagine no-image debug dump\n")
                f.write(f"# placeholder_count={placeholder_count} image_urls={image_urls}\n")
                f.write(f"# request_message_head={message[:300]!r}\n")
                f.write("# ===== raw SSE lines =====\n")
                for ln in raw_lines:
                    f.write(ln.rstrip("\n") + "\n")
            logger.warning("chat_imagine: dumped raw SSE to %s", dbg_path)
        except Exception as _e:
            logger.warning("chat_imagine: failed to dump raw SSE: %s", _e)
        llm_text = "".join(text_chunks).strip()
        snippet = (llm_text[:300] + "…") if len(llm_text) > 300 else llm_text
        # 区分两种"无图"情况：
        #  A) LLM 真拒绝：placeholder_count=0，LLM 输出"I'm sorry, I must decline..."
        #  B) Grok 内容审核拦截：placeholder_count>0，LLM 已下发生图意图（cardAttachment 占位）
        #     但 grok 后端的 imagine pipeline 没产出 image_chunk(progress=100) — 后置审核拦了
        if placeholder_count > 0:
            msg = (f"Grok 内容审核拦截（LLM 已下发 {placeholder_count} 张图的生图意图，"
                   f"但 imagine 后端未产出实际图片，多见于敏感内容触发后置审核）")
            code = "image_moderated"
        else:
            msg = "LLM 拒绝：未下发任何生图意图（safety/decline）"
            code = "chat_no_image"
        if snippet:
            msg += f" — LLM 回复：{snippet}"
        logger.warning("chat_imagine: no images, placeholders=%d llm_text_len=%d snippet=%s",
                       placeholder_count, len(llm_text), snippet[:120])
        raise GrokClientError(msg, status_code=403, code=code)
    return image_urls, "".join(text_chunks).strip()


async def query_rate_limits(settings: Settings, *, model_name: str) -> dict[str, Any]:
    """查询指定 model 的 chat 配额（POST /rest/rate-limits）。

    Response 示例（正常）: {"windowSizeSeconds":7200,"remainingQueries":38,"totalQueries":50,
                          "lowEffortRateLimits":null,"highEffortRateLimits":null}
    Response 示例（限速）: {"windowSizeSeconds":7200,"remainingQueries":0,"waitTimeSeconds":2429,
                          "totalQueries":25,"lowEffortRateLimits":null,"highEffortRateLimits":null}
    waitTimeSeconds 仅在 remainingQueries==0 时出现，表示距配额重置的剩余秒数。
    """
    if not settings.grok_cookie:
        raise GrokClientError("GROK_COOKIE is not configured", status_code=400, code="missing_grok_cookie")
    payload = json.dumps({"modelName": model_name}).encode("utf-8")
    headers = _headers(settings)
    headers["Content-Type"] = "application/json"
    async with AsyncSession(**_session_kwargs(settings)) as session:
        try:
            resp = await session.post(_RATE_LIMITS_URL, headers=headers, data=payload, timeout=30.0)
        except Exception as exc:
            raise GrokClientError(f"rate-limits request failed: {exc}", status_code=502) from exc
        body = resp.content.decode("utf-8", "replace")
        if resp.status_code != 200:
            if _contains_cloudflare_challenge(resp.status_code, body):
                raise GrokClientError("Cloudflare challenge", status_code=403, code="cloudflare_challenge")
            raise GrokClientError(f"rate-limits returned {resp.status_code}", status_code=resp.status_code, body=body[:200])
        try:
            return json.loads(body)
        except Exception as exc:
            raise GrokClientError(f"rate-limits parse failed: {exc}", status_code=502) from exc


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

    full_body = response.content.decode("utf-8", "replace")
    body = full_body[:400]
    if response.status_code != 200:
        if _contains_cloudflare_challenge(response.status_code, full_body[:8192]):
            raise GrokClientError(
                "Cloudflare 拦截（cf_clearance 失效）— 请确认 FlareSolverr 在线且与 xGate 使用同一出口 IP",
                status_code=403,
                body=body,
                code="cloudflare_challenge",
            )
        if response.status_code == 403:
            raise GrokClientError(
                "Grok 返回 403 — Cookie 可能已失效或被 Cloudflare 拦截，请重新从浏览器导入 cURL",
                status_code=403,
                body=body,
                code="upstream_403",
            )
        raise GrokClientError(
            f"Grok skills smoke 返回 {response.status_code}",
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


_SILENT_BLOCK_GRACE_SECONDS = 30.0  # 无任何 slot 帧时的早期放弃阈值


@dataclass
class BatchOutcome:
    results: list[ImageResult]
    ws_closed: bool
    total_slots: int
    moderated_count: int
    r_rated_count: int


async def _collect_batch(
    ws: aiohttp.ClientWebSocketResponse,
    deadline: float,
    *,
    batch_probe_s: float = 3.0,
    session_dir: Path,
) -> BatchOutcome:
    """
    从已发送请求的 WS 上收一批图片。

    协议：
    - start_stage 帧 → 注册 slot
    - image 帧（中间预览）→ 缓存 blob/url，不保存
    - completed 帧 → 保存成品；moderated:true 标记 slot.moderated 跳过
    - 所有已知 slot 都 completed 后，再探测 batch_probe_s 秒看有没有新 start_stage
    """
    slots: dict[str, _ImgSlot] = {}
    results: list[ImageResult] = []
    moderated_count = 0
    r_rated_count = 0
    all_done_since: float | None = None
    started_at = asyncio.get_event_loop().time()

    while True:
        now = asyncio.get_event_loop().time()
        if now >= deadline:
            break

        # silent-block 早期检测：无任何 slot 帧 + 已等待 _SILENT_BLOCK_GRACE_SECONDS
        if not slots and (now - started_at) >= _SILENT_BLOCK_GRACE_SECONDS:
            logger.warning(
                "imagine batch: no frames received within %.0fs — likely silent moderation block",
                _SILENT_BLOCK_GRACE_SECONDS,
            )
            raise GrokClientError(
                "Imagine likely blocked by content moderation (no response from upstream)",
                status_code=403, code="silent_block",
            )

        # 如果所有已知 slot 都完成，进入批次结束探测
        if slots and all(s.done for s in slots.values()):
            if all_done_since is None:
                all_done_since = now
            probe_remaining = batch_probe_s - (now - all_done_since)
            if probe_remaining <= 0:
                return BatchOutcome(results, False, len(slots), moderated_count, r_rated_count)
            recv_timeout = min(probe_remaining, deadline - now)
        else:
            all_done_since = None
            recv_timeout = min(15.0, deadline - now)

        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=recv_timeout)
        except asyncio.TimeoutError:
            if slots and all(s.done for s in slots.values()):
                return BatchOutcome(results, False, len(slots), moderated_count, r_rated_count)
            continue

        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
            return BatchOutcome(results, True, len(slots), moderated_count, r_rated_count)

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
                    moderated_count += 1
                    logger.warning("imagine slot moderated: image_id=%s", image_id[:8])
                    continue
                if data.get("r_rated"):
                    r_rated_count += 1
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

    return BatchOutcome(results, False, len(slots), moderated_count, r_rated_count)


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
_GROK_DELETE_URL_BASE = "https://grok.com/rest/assets-metadata"
_GROK_ASSETS_DL_BASE = "https://assets.grok.com"
GROK_FILES_DIR = Path("data/grok-files")


def _files_headers(settings: Settings) -> dict[str, str]:
    h = _headers(settings)
    h["Referer"] = "https://grok.com/files"
    h.pop("Content-Type", None)
    return h


def _dl_headers(settings: Settings) -> dict[str, str]:
    """用于从 assets.grok.com 下载文件的请求头（跨域 CDN，Sec-Fetch-Site=cross-site）。"""
    h = _headers(settings)
    h["Referer"] = "https://grok.com/"
    h["Sec-Fetch-Site"] = "cross-site"
    h["Sec-Fetch-Dest"] = "document"
    h["Sec-Fetch-Mode"] = "navigate"
    h.pop("Content-Type", None)
    h.pop("Origin", None)
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


async def delete_grok_asset(settings: Settings, asset_id: str) -> bool:
    """删除 Grok 云端 asset。

    真实 API：`DELETE /rest/assets-metadata/{asset_id}`（无 body）。

    返回值：
    - `True`  — API 返回 200，确认已从云端删除（应写入 cloud_deleted_at）
    - `False` — API 返回 404/410，asset 已不在云端或不存在；调用方**不要**写
                 cloud_deleted_at（避免误标），由后续全量同步校准
    抛 GrokClientError — 其他失败（5xx、Cloudflare 挑战、网络错误）
    """
    if not settings.grok_cookie:
        raise GrokClientError("GROK_COOKIE is not configured", status_code=400, code="missing_grok_cookie")
    if not asset_id:
        raise GrokClientError("empty asset_id", status_code=400, code="bad_request")
    url = f"{_GROK_DELETE_URL_BASE}/{asset_id}"
    h = _files_headers(settings)
    h.pop("Content-Type", None)  # DELETE 无 body
    async with AsyncSession(**_session_kwargs(settings)) as sess:
        try:
            resp = await sess.request("DELETE", url, headers=h, timeout=30.0)
            body = resp.content.decode("utf-8", "replace")
            if resp.status_code == 200:
                return True
            if resp.status_code in (404, 410):
                logger.info("delete_grok_asset: %s returned %d (not on cloud)", asset_id, resp.status_code)
                return False
            if _contains_cloudflare_challenge(resp.status_code, body):
                raise GrokClientError("Cloudflare challenge", status_code=403, code="cloudflare_challenge")
            raise GrokClientError(
                f"Delete returned {resp.status_code}", status_code=resp.status_code, body=body[:200]
            )
        except GrokClientError:
            raise
        except Exception as exc:
            raise GrokClientError(f"Delete asset failed: {exc}", status_code=502) from exc


async def stream_grok_asset(settings: Settings, key: str):
    """从 assets.grok.com 流式下载文件，返回 (content_type, async_generator)。"""
    if not settings.grok_cookie:
        raise GrokClientError("GROK_COOKIE is not configured", status_code=400, code="missing_grok_cookie")
    url = f"{_GROK_ASSETS_DL_BASE}/{key}"
    dl_headers = _dl_headers(settings)
    sess = AsyncSession(**_session_kwargs(settings))
    try:
        resp = await sess.get(url, headers=dl_headers, timeout=120.0, stream=True)
        if resp.status_code not in (200, 206):
            await sess.close()
            raise GrokClientError(
                f"Asset download returned {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text[:200],
            )
        content_type = resp.headers.get("Content-Type", "application/octet-stream")

        async def _gen():
            try:
                async for chunk in resp.aiter_content(65536):
                    yield chunk
            finally:
                await sess.close()

        return content_type, _gen()
    except GrokClientError:
        await sess.close()
        raise
    except Exception as exc:
        await sess.close()
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


async def save_grok_asset_local(
    settings: Settings, key: str, filename: str, size_bytes: int = 0
) -> tuple[str, bool]:
    """从 assets.grok.com 下载文件并保存到 data/grok-files/。

    Returns:
        (path, skipped) — skipped=True 表示文件已存在且大小一致，直接复用。
    """
    if not settings.grok_cookie:
        raise GrokClientError("GROK_COOKIE is not configured", status_code=400, code="missing_grok_cookie")
    if not key:
        raise GrokClientError("Asset key is empty", status_code=400, code="missing_key")
    GROK_FILES_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[^\w.\-]', '_', filename)[:200] or "file"
    dest = GROK_FILES_DIR / safe_name

    # 文件存在且大小一致 → 跳过；大小不一致 → 重新下载（上次下载不完整）
    if dest.exists():
        if size_bytes <= 0 or dest.stat().st_size == size_bytes:
            logger.info("grok file already exists, skipping: %s", dest)
            return str(dest), True
        logger.info("grok file size mismatch (%d vs %d), re-downloading: %s",
                    dest.stat().st_size, size_bytes, dest)

    url = f"{_GROK_ASSETS_DL_BASE}/{key}"
    async with AsyncSession(**_session_kwargs(settings)) as sess:
        try:
            resp = await sess.get(url, headers=_dl_headers(settings), timeout=120.0)
            if resp.status_code not in (200, 206):
                raise GrokClientError(
                    f"Asset download returned {resp.status_code}",
                    status_code=resp.status_code,
                    body=resp.text[:200],
                )
            data = resp.content
        except GrokClientError:
            raise
        except Exception as exc:
            raise GrokClientError(f"Asset download failed: {exc}", status_code=502) from exc

    if not data:
        raise GrokClientError("Asset download returned empty file", status_code=502, code="empty_file")
    dest.write_bytes(data)
    logger.info("grok file saved: %s (%d bytes)", dest, len(data))
    return str(dest), False
