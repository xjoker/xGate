"""xGate MCP Streamable HTTP 服务端。

暴露 Grok 网页能力为 MCP tools，支持：
  grok_chat             — 多轮对话（含搜索结果 / 引用 / 推理步骤）
  grok_x_search         — X 搜索（纯数据，不含 LLM 总结）
  grok_web_search       — Web 搜索（纯数据）
  grok_quota            — 配额查询
  grok_imagine          — 图片生成（chat 通道）
  grok_imagine_video    — 视频生成
  grok_files_list       — 列出 Grok Files
  grok_files_save_local — 下载 Grok Files 到本地
  grok_files_delete     — 删除 Grok Files

所有 tool 返回 buffer 模式（符合 MCP 2025-06-18 spec，主流客户端均不增量渲染 tool 输出）。
流式诉求请打 /v1/chat/completions?stream=true。
"""
from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote as _url_quote

from mcp.server.fastmcp import FastMCP, Context
from mcp.server.transport_security import TransportSecuritySettings

from .config import Settings
from .grok_client import (
    GrokCitation,
    GrokClientError,
    GrokConversationStarted,
    GrokDone,
    GrokFinalToken,
    GrokReasoningHeader,
    GrokReasoningToken,
    GrokToolCall,
    GrokWebSearchResults,
    GrokXSearchResults,
    IMAGE_DIR,
    chat_imagine,
    create_video,
    delete_grok_asset,
    get_video_link,
    list_grok_assets,
    query_rate_limits,
    save_grok_asset_local,
    stream_chat_events,
    stream_grok_asset,
)
from .models import get_model
from .db import log_db
from . import mcp_session

logger = logging.getLogger(__name__)

_get_settings: Callable[[], Settings] | None = None

mcp = FastMCP(
    "xgate",
    instructions=(
        "Access Grok's web capabilities via xGate. "
        "Use grok_chat for conversation, grok_x_search for X posts, "
        "grok_web_search for web results, grok_quota to check rate limits."
    ),
    # FastAPI mounts at /mcp and strips the prefix, so inner route must be /
    streamable_http_path="/",
    # Disable DNS rebinding protection — xGate's _BearerAuthMiddleware handles auth.
    # Without this, FastMCP default host="127.0.0.1" blocks any non-localhost client (421).
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def configure(get_settings: Callable[[], Settings]) -> None:
    global _get_settings
    _get_settings = get_settings


def _settings() -> Settings:
    if _get_settings is None:
        raise RuntimeError("mcp_server.configure() not called")
    return _get_settings()


# ── Query builder ──────────────────────────────────────────────────────────────

def _build_x_query(
    query: str, *,
    from_users: list[str], exclude_users: list[str],
    since: str | None, until: str | None, within_time: str | None,
    min_faves: int | None, min_retweets: int | None, min_replies: int | None,
    lang: str | None, exclude_retweets: bool, exclude_replies: bool,
    media: str | None, verified_only: bool, raw_query: str | None,
) -> str:
    parts = [query.strip()]
    if from_users:
        parts.append(f"({' OR '.join(f'from:{u}' for u in from_users)})")
    for u in exclude_users:
        parts.append(f"-from:{u}")
    if since:
        parts.append(f"since:{since}")
    if until:
        parts.append(f"until:{until}")
    elif within_time and not since:
        parts.append(f"within_time:{within_time}")
    if min_faves is not None:
        parts.append(f"min_faves:{min_faves}")
    if min_retweets is not None:
        parts.append(f"min_retweets:{min_retweets}")
    if min_replies is not None:
        parts.append(f"min_replies:{min_replies}")
    if lang:
        parts.append(f"lang:{lang}")
    if exclude_retweets:
        parts.append("-filter:retweets")
    if exclude_replies:
        parts.append("-filter:replies")
    if media in ("images", "videos", "links"):
        parts.append(f"filter:{media}")
    if verified_only:
        parts.append("filter:blue_verified")
    if raw_query:
        parts.append(raw_query.strip())
    return " ".join(parts)


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
async def grok_chat(
    prompt: str,
    ctx: Context,
    model: str = "grok-4.20-auto",
    conversation_id: str | None = None,
    parent_response_id: str | None = None,
    format: Literal["rich", "openai"] = "rich",
    include_reasoning: bool = False,
    include_search_results: bool = True,
    disable_search: bool = False,
    temporary: bool = True,
) -> dict:
    """与 Grok 对话，返回完整答复 + 搜索结果 / 引用 / 推理步骤。

    支持自动续轮：同一 MCP session 内不传 conversation_id 时自动接上一轮。
    显式传 conversation_id="" 强制开新会话。
    """
    settings = _settings()
    sid = str(id(ctx.session))
    _t0 = time.time()
    _rid = uuid.uuid4().hex
    _status = "success"
    _err: str | None = None

    if conversation_id == "":
        conv_id: str | None = None
        par_id: str | None = None
    elif conversation_id is None:
        conv_id, par_id = mcp_session.get(sid)
        if parent_response_id:
            par_id = parent_response_id
    else:
        conv_id = conversation_id
        par_id = parent_response_id

    spec = get_model(model)
    mode_id = spec.mode_id if spec else "auto"

    result_conv_id = conv_id or ""
    result_resp_id = ""
    text_parts: list[str] = []
    reasoning_steps: list[dict] = []
    reasoning_tokens_list: list[dict] = []
    tool_calls_list: list[dict] = []
    x_results: list[dict] = []
    web_results: list[dict] = []
    citations: list[dict] = []
    follow_ups: list[str] = []
    rollouts_used: set[str] = set()

    try:
        async for event in stream_chat_events(
            settings, message=prompt, mode_id=mode_id,
            conversation_id=conv_id, parent_response_id=par_id,
            disable_search=disable_search, temporary=temporary,
        ):
            if isinstance(event, GrokConversationStarted):
                result_conv_id = event.conversation_id
            elif isinstance(event, GrokFinalToken):
                text_parts.append(event.token)
            elif isinstance(event, GrokReasoningHeader):
                rollouts_used.add(event.rollout)
                reasoning_steps.append({"step": event.step_id, "rollout": event.rollout, "label": event.label})
            elif isinstance(event, GrokReasoningToken):
                rollouts_used.add(event.rollout)
                if include_reasoning:
                    reasoning_tokens_list.append({"rollout": event.rollout, "token": event.token})
            elif isinstance(event, GrokToolCall):
                tool_calls_list.append({"tool": event.tool, "args": event.args})
            elif isinstance(event, GrokXSearchResults):
                x_results.extend(event.results)
            elif isinstance(event, GrokWebSearchResults):
                web_results.extend(event.results)
            elif isinstance(event, GrokCitation):
                citations.append({"card_id": event.card_id, "url": event.url})
            elif isinstance(event, GrokDone):
                result_resp_id = event.response_id or result_resp_id
                if event.follow_up_suggestions:
                    follow_ups = list(event.follow_up_suggestions)
    except GrokClientError as exc:
        _status, _err = "error", str(exc)
        log_db.log_mcp(request_id=_rid, tool="grok_chat", model=model, prompt=prompt[:500],
                       status=_status, duration_ms=int((time.time() - _t0) * 1000), error=_err)
        return {"error": str(exc), "code": exc.code}

    text = "".join(text_parts)
    if sid and result_conv_id and result_resp_id:
        mcp_session.upsert(sid, result_conv_id, result_resp_id)

    log_db.log_mcp(request_id=_rid, tool="grok_chat", model=model, prompt=prompt[:500],
                   response=text[:200], status=_status, duration_ms=int((time.time() - _t0) * 1000))

    if format == "openai":
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text, "refusal": None}, "finish_reason": "stop"}],
            "usage": None,
            "metadata": {
                "conversation_id": result_conv_id,
                "response_id": result_resp_id,
                "x_results": x_results if include_search_results else [],
                "web_results": web_results if include_search_results else [],
                "citations": citations,
                "follow_ups": follow_ups,
            },
        }

    return {
        "conversation_id": result_conv_id,
        "response_id": result_resp_id,
        "title": None,
        "text": text,
        "reasoning_steps": reasoning_steps,
        "reasoning_tokens": reasoning_tokens_list,
        "tool_calls": tool_calls_list,
        "x_results": x_results if include_search_results else [],
        "web_results": web_results if include_search_results else [],
        "citations": citations,
        "follow_ups": follow_ups,
        "metadata": {"model": model, "rollouts_used": sorted(rollouts_used)},
    }


@mcp.tool()
async def grok_x_search(
    query: str,
    from_users: list[str] | None = None,
    exclude_users: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    within_time: str | None = None,
    min_faves: int | None = None,
    min_retweets: int | None = None,
    min_replies: int | None = None,
    lang: str | None = None,
    exclude_retweets: bool = True,
    exclude_replies: bool = False,
    media: str | None = None,
    verified_only: bool = False,
    raw_query: str | None = None,
    limit: int = 10,
) -> dict:
    """X 搜索（不带 LLM 总结），返回原始结构化推文列表。

    支持高级筛选：时间范围、最小点赞/转推数、语言、媒体类型、认证账号过滤等。
    limit 最大 30。
    """
    settings = _settings()
    _t0 = time.time(); _rid = uuid.uuid4().hex
    query_str = _build_x_query(
        query,
        from_users=from_users or [], exclude_users=exclude_users or [],
        since=since, until=until, within_time=within_time,
        min_faves=min_faves, min_retweets=min_retweets, min_replies=min_replies,
        lang=lang, exclude_retweets=exclude_retweets, exclude_replies=exclude_replies,
        media=media, verified_only=verified_only, raw_query=raw_query,
    )
    limit = max(1, min(30, limit))
    prompt = f"Use xSearch to find posts matching exactly this query (do not modify the query): {query_str}"

    all_results: list[dict] = []
    try:
        async for event in stream_chat_events(settings, message=prompt, mode_id="auto", temporary=True):
            if isinstance(event, GrokXSearchResults):
                all_results.extend(event.results)
            elif isinstance(event, GrokDone):
                break
    except GrokClientError as exc:
        log_db.log_mcp(request_id=_rid, tool="grok_x_search", prompt=query[:500],
                       status="error", duration_ms=int((time.time()-_t0)*1000), error=str(exc))
        return {"error": str(exc), "code": exc.code, "query_used": query_str}

    normalized = []
    for r in all_results[:limit]:
        post_id = r.get("postId", "")
        username = r.get("username", "")
        normalized.append({
            "username": username,
            "name": r.get("name", ""),
            "text": r.get("text", ""),
            "post_id": post_id,
            "post_url": f"https://x.com/{username}/status/{post_id}" if post_id and username else "",
            "create_time": r.get("createTime", ""),
            "profile_image_url": r.get("profileImageUrl", ""),
            "view_count": r.get("viewCount", 0),
            "community_note": r.get("communityNote", ""),
            "quote": r.get("quote"),
            "parent": r.get("parent"),
        })
    log_db.log_mcp(request_id=_rid, tool="grok_x_search", prompt=query[:500],
                   status="success", duration_ms=int((time.time()-_t0)*1000))
    return {"query_used": query_str, "result_count": len(normalized), "results": normalized}


@mcp.tool()
async def grok_web_search(
    query: str,
    recency_days: int | None = None,
    site: str | None = None,
    allowed_domains: list[str] | None = None,
    excluded_domains: list[str] | None = None,
    raw_query: str | None = None,
    limit: int = 10,
) -> dict:
    """Web 搜索，返回原始结构化结果（url / title / preview）。limit 最大 30。"""
    settings = _settings()
    _t0 = time.time(); _rid = uuid.uuid4().hex

    parts = [query.strip()]
    if site:
        parts.append(f"site:{site}")
    if raw_query:
        parts.append(raw_query.strip())
    query_str = " ".join(parts)

    prompt_parts = [f"Use webSearch to search for: {query_str}"]
    if recency_days:
        prompt_parts.append(f"Limit results to last {recency_days} days.")
    if allowed_domains:
        prompt_parts.append(f"Only include results from: {', '.join(allowed_domains)}.")
    if excluded_domains:
        prompt_parts.append(f"Exclude results from: {', '.join(excluded_domains)}.")
    prompt = " ".join(prompt_parts)

    limit = max(1, min(30, limit))
    all_results: list[dict] = []
    try:
        async for event in stream_chat_events(settings, message=prompt, mode_id="auto", disable_search=False, temporary=True):
            if isinstance(event, GrokWebSearchResults):
                all_results.extend(event.results)
            elif isinstance(event, GrokDone):
                break
    except GrokClientError as exc:
        log_db.log_mcp(request_id=_rid, tool="grok_web_search", prompt=query[:500],
                       status="error", duration_ms=int((time.time()-_t0)*1000), error=str(exc))
        return {"error": str(exc), "code": exc.code, "query_used": query_str}

    results = all_results[:limit]
    log_db.log_mcp(request_id=_rid, tool="grok_web_search", prompt=query[:500],
                   status="success", duration_ms=int((time.time()-_t0)*1000))
    return {"query_used": query_str, "result_count": len(results), "results": results}


@mcp.tool()
async def grok_quota(model: str = "grok-4.20-auto") -> dict:
    """查询指定模型的剩余配额（remainingQueries / totalQueries / windowSizeSeconds）。"""
    settings = _settings()
    _t0 = time.time(); _rid = uuid.uuid4().hex
    spec = get_model(model)
    mode_id = spec.mode_id if spec else model
    try:
        raw = await query_rate_limits(settings, model_name=mode_id)
        log_db.log_mcp(request_id=_rid, tool="grok_quota", model=model,
                       status="success", duration_ms=int((time.time()-_t0)*1000))
        return {
            "model": model,
            "window_size_seconds": raw.get("windowSizeSeconds"),
            "remaining_queries": raw.get("remainingQueries"),
            "total_queries": raw.get("totalQueries"),
        }
    except GrokClientError as exc:
        log_db.log_mcp(request_id=_rid, tool="grok_quota", model=model,
                       status="error", duration_ms=int((time.time()-_t0)*1000), error=str(exc))
        return {"error": str(exc), "code": exc.code, "model": model}


# ── 图片 / 视频 / 文件 helpers ─────────────────────────────────────────────────

def _proxy_url(assets_url: str) -> str:
    """将 assets.grok.com URL 转为 xGate /v1/files/proxy 代理 URL。"""
    s = _settings()
    if s.public_base_url:
        base = s.public_base_url.rstrip("/")
    else:
        host = "localhost" if s.server_host in ("0.0.0.0", "") else s.server_host
        base = f"http://{host}:{s.server_port}"
    return f"{base}/v1/files/proxy?url={_url_quote(assets_url, safe='')}"


async def _fetch_bytes(settings: Settings, assets_url: str) -> bytes:
    key = assets_url.split("assets.grok.com/", 1)[-1] if "assets.grok.com/" in assets_url else assets_url.lstrip("/")
    _, gen = await stream_grok_asset(settings, key)
    return b"".join([chunk async for chunk in gen])


# ── Tools: 图片 / 视频 / 文件 ──────────────────────────────────────────────────

@mcp.tool()
async def grok_imagine(
    prompt: str,
    n: int = 2,
    aspect_ratio: Literal["1:1", "16:9", "9:16", "3:2", "2:3", "4:3", "3:4"] = "1:1",
    return_mode: Literal["url", "local_path", "base64"] = "url",
) -> dict:
    """通过 Grok 生成图片（chat 通道）。

    return_mode="url"   → 返回 xGate 代理 URL，MCP 客户端无需 Cookie 即可访问。
    return_mode="local_path" → 保存到 data/images/mcp/ 并返回本地路径。
    return_mode="base64"    → 内嵌 base64（小图可用，大图会使 tool 响应体膨胀）。
    """
    settings = _settings()
    _t0 = time.time(); _rid = uuid.uuid4().hex
    n = max(1, min(10, n))
    try:
        image_urls, _ = await chat_imagine(
            settings, message=prompt, mode_id="fast",
            image_count=n, aspect_ratio=aspect_ratio,
        )
    except GrokClientError as exc:
        log_db.log_mcp(request_id=_rid, tool="grok_imagine", prompt=prompt[:500],
                       status="error", duration_ms=int((time.time()-_t0)*1000), error=str(exc))
        return {"error": str(exc), "code": exc.code, "moderation": exc.code == "image_moderated"}

    session_id = f"mcp-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    images: list[dict] = []
    moderation = "passed"

    for url in image_urls:
        item: dict[str, Any] = {"url": None, "local_path": None, "base64": None}
        try:
            if return_mode == "url":
                item["url"] = _proxy_url(url)
            elif return_mode == "local_path":
                save_dir = IMAGE_DIR / "mcp" / session_id
                save_dir.mkdir(parents=True, exist_ok=True)
                filename = url.split("/")[-1].split("?")[0] or f"image-{uuid.uuid4().hex[:8]}.jpg"
                key = url.split("assets.grok.com/", 1)[-1] if "assets.grok.com/" in url else url.lstrip("/")
                dest = save_dir / filename
                data = await _fetch_bytes(settings, url)
                dest.write_bytes(data)
                item["local_path"] = str(dest)
            elif return_mode == "base64":
                data = await _fetch_bytes(settings, url)
                item["base64"] = base64.b64encode(data).decode()
        except Exception as exc:
            item["error"] = str(exc)
        images.append(item)

    log_db.log_mcp(request_id=_rid, tool="grok_imagine", prompt=prompt[:500],
                   status="success", duration_ms=int((time.time()-_t0)*1000))
    return {
        "session_id": session_id,
        "moderation": moderation,
        "rephrased_prompt": None,
        "images": images,
    }


@mcp.tool()
async def grok_imagine_video(
    prompt: str,
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "16:9",
    duration_seconds: Literal[5, 10, 15] = 5,
    return_mode: Literal["url", "local_path"] = "url",
) -> dict:
    """通过 Grok 生成视频（下载后本地缓存）。耗时较长（通常 1-5 分钟），请耐心等待。"""
    settings = _settings()
    _t0 = time.time(); _rid = uuid.uuid4().hex
    session_id = f"mcp-video-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    try:
        post_id = await create_video(
            settings, prompt=prompt, aspect_ratio=aspect_ratio,
            duration=duration_seconds, session_id=session_id,
        )
    except GrokClientError as exc:
        log_db.log_mcp(request_id=_rid, tool="grok_imagine_video", prompt=prompt[:500],
                       status="error", duration_ms=int((time.time()-_t0)*1000), error=str(exc))
        return {"error": str(exc), "code": exc.code}

    serve_path = await get_video_link(settings, post_id)
    log_db.log_mcp(request_id=_rid, tool="grok_imagine_video", prompt=prompt[:500],
                   status="success", duration_ms=int((time.time()-_t0)*1000))
    if return_mode == "local_path":
        local = IMAGE_DIR / session_id / f"{post_id}.mp4"
        return {"video_url": None, "local_path": str(local), "duration_seconds": duration_seconds}

    if serve_path:
        s = _settings()
        if s.public_base_url:
            base = s.public_base_url.rstrip("/")
        else:
            host = "localhost" if s.server_host in ("0.0.0.0", "") else s.server_host
            base = f"http://{host}:{s.server_port}"
        video_url = f"{base}{serve_path}"
    else:
        video_url = None
    return {"video_url": video_url, "local_path": None, "duration_seconds": duration_seconds}


@mcp.tool()
async def grok_files_list(
    limit: int = 50,
    offset: int = 0,
    kind: Literal["image", "video", "all"] = "all",
) -> dict:
    """列出 Grok Files（从云端实时查询）。kind 可按 image / video / all 过滤。"""
    settings = _settings()
    _t0 = time.time(); _rid = uuid.uuid4().hex
    try:
        raw = await list_grok_assets(settings, page_size=min(limit + offset, 200))
    except GrokClientError as exc:
        log_db.log_mcp(request_id=_rid, tool="grok_files_list",
                       status="error", duration_ms=int((time.time()-_t0)*1000), error=str(exc))
        return {"error": str(exc), "code": exc.code}

    assets = raw.get("assets") or []
    items: list[dict] = []
    for a in assets:
        mime = a.get("mimeType", "")
        file_kind = "image" if mime.startswith("image/") else "video" if mime.startswith("video/") else "other"
        if kind != "all" and file_kind != kind:
            continue
        key = a.get("key", "")
        items.append({
            "file_id": a.get("assetId", ""),
            "name": a.get("name", ""),
            "kind": file_kind,
            "size_bytes": int(a.get("sizeBytes") or 0),
            "created_at": a.get("createTime", ""),
            "url": _proxy_url(f"https://assets.grok.com/{key}") if key else "",
        })

    page = items[offset:offset + limit]
    log_db.log_mcp(request_id=_rid, tool="grok_files_list",
                   status="success", duration_ms=int((time.time()-_t0)*1000))
    return {"items": page, "total": len(items)}


@mcp.tool()
async def grok_files_save_local(file_ids: list[str]) -> dict:
    """将 Grok Files 下载保存到本地 data/grok-files/。传入 grok_files_list 返回的 file_id 列表。"""
    settings = _settings()
    _t0 = time.time(); _rid = uuid.uuid4().hex
    try:
        raw = await list_grok_assets(settings, page_size=200)
    except GrokClientError as exc:
        log_db.log_mcp(request_id=_rid, tool="grok_files_save_local", prompt=str(file_ids)[:200],
                       status="error", duration_ms=int((time.time()-_t0)*1000), error=str(exc))
        return {"error": f"list failed: {exc}", "saved": [], "failed": []}

    id_map = {a.get("assetId", ""): a for a in (raw.get("assets") or [])}
    saved: list[dict] = []
    failed: list[dict] = []

    for fid in file_ids:
        asset = id_map.get(fid)
        if not asset:
            failed.append({"file_id": fid, "error": "not found in cloud listing"})
            continue
        key = asset.get("key", "")
        name = asset.get("name", "file")
        size = int(asset.get("sizeBytes") or 0)
        try:
            local_path, _ = await save_grok_asset_local(settings, key, name, size)
            saved.append({"file_id": fid, "local_path": local_path})
        except GrokClientError as exc:
            failed.append({"file_id": fid, "error": str(exc)})

    status = "success" if not failed else ("error" if not saved else "success")
    log_db.log_mcp(request_id=_rid, tool="grok_files_save_local", prompt=str(file_ids)[:200],
                   status=status, duration_ms=int((time.time()-_t0)*1000))
    return {"saved": saved, "failed": failed}


@mcp.tool()
async def grok_files_delete(file_ids: list[str]) -> dict:
    """从 Grok 云端删除文件。传入 grok_files_list 返回的 file_id 列表。"""
    settings = _settings()
    _t0 = time.time(); _rid = uuid.uuid4().hex
    deleted: list[str] = []
    failed: list[dict] = []

    for fid in file_ids:
        try:
            ok = await delete_grok_asset(settings, fid)
            if ok:
                deleted.append(fid)
            else:
                failed.append({"file_id": fid, "error": "not found on cloud (404/410)"})
        except GrokClientError as exc:
            failed.append({"file_id": fid, "error": str(exc)})

    status = "success" if not failed else ("error" if not deleted else "success")
    log_db.log_mcp(request_id=_rid, tool="grok_files_delete", prompt=str(file_ids)[:200],
                   status=status, duration_ms=int((time.time()-_t0)*1000))
    return {"deleted": deleted, "failed": failed}


# ── Bearer auth ASGI middleware ────────────────────────────────────────────────

class _BearerAuthMiddleware:
    """校验 Authorization: Bearer <api_key>。"""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            headers: dict[bytes, bytes] = dict(scope.get("headers", []))

            api_key = _settings().api_key
            if api_key:
                auth = headers.get(b"authorization", b"").decode("utf-8", "replace")
                if not (auth.startswith("Bearer ") and auth[7:].strip() == api_key):
                    body = json.dumps({"error": "Unauthorized", "code": "invalid_api_key"}).encode()
                    await send({"type": "http.response.start", "status": 401,
                                "headers": [(b"content-type", b"application/json")]})
                    await send({"type": "http.response.body", "body": body, "more_body": False})
                    return

        await self._app(scope, receive, send)


def create_mcp_app(get_settings: Callable[[], Settings]) -> Any:
    """创建 Streamable HTTP MCP ASGI 应用（含 Bearer 鉴权中间件）。调用后 mcp.session_manager 可用。"""
    configure(get_settings)
    inner = mcp.streamable_http_app()
    return _BearerAuthMiddleware(inner)


def create_sse_app() -> Any:
    """创建 SSE MCP ASGI 应用（供 mcp-remote 等 stdio→SSE 桥接客户端使用）。

    挂载于 /mcp/sse（GET）和 /mcp/messages（POST），与 Streamable HTTP 端点 /mcp 并存。
    """
    inner = mcp.sse_app()
    return _BearerAuthMiddleware(inner)
