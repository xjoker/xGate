"""Grok 云端 Files API 客户端 — 从 grok_client.py 剥离的自闭合岛 (v0.3.8)。

advisor 评估 (2026-05-17) 给出方案 B：仅拆 grok_files 这一组自闭合函数，
保留 grok_client.py 其余部分（chat/imagine/video/auth 深度共享 helper，
强行拆会触发循环 import）。这里 4 个 async 函数 + 2 个 header helper：

- list_grok_assets
- delete_grok_asset
- stream_grok_asset
- save_grok_asset_local

依赖 grok_client 的私有 helper（`_headers` / `_session_kwargs` /
`_contains_cloudflare_challenge`），暂时维持跨模块借用 — Yuki memory
9398f8c5 后续重构如果公开化这些 helper，本模块同步切换 import path。

`grok_client.py` 通过 `from .grok_files_client import *` re-export 这些符号，
现有 caller (main.py / mcp_server.py) 无需改 import path。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from curl_cffi.requests import AsyncSession

# 跨模块借用：保持 grok_client 私有 helper（advisor 标注 known violation）
from .config import Settings
from .grok_client import (
    GrokClientError,
    _contains_cloudflare_challenge,
    _headers,
    _session_kwargs,
)

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────────

_GROK_ASSETS_URL = "https://grok.com/rest/assets"
_GROK_DELETE_URL_BASE = "https://grok.com/rest/assets-metadata"
_GROK_ASSETS_DL_BASE = "https://assets.grok.com"
GROK_FILES_DIR = Path("data/grok-files")


# ── header helpers ────────────────────────────────────────────────────────────

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


# ── Grok 云端 Files API ────────────────────────────────────────────────────────

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
