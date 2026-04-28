"""从浏览器 HAR 中提取 Grok Cookie、User-Agent 和 curl_cffi 指纹。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, get_args
from urllib.parse import urlparse


class HarImportError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class HarImportResult:
    cookie: str
    user_agent: str
    browser: str
    source_url: str


def parse_grok_har(raw: bytes, *, max_bytes: int = 25 * 1024 * 1024) -> HarImportResult:
    if len(raw) > max_bytes:
        raise HarImportError("HAR 文件过大，最大支持 25 MiB")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarImportError("无法解析 HAR，请确认文件来自 Chrome 或 Edge 导出") from exc

    entries = ((data.get("log") or {}).get("entries") or [])
    if not isinstance(entries, list):
        raise HarImportError("HAR 缺少 log.entries")

    best: tuple[int, str, str, str] | None = None
    fallback_user_agent = ""
    fallback_url = ""
    for entry in entries:
        request = (entry or {}).get("request") or {}
        if not isinstance(request, dict):
            continue
        url = str(request.get("url") or "")
        headers = _headers_map(request.get("headers") or [])
        user_agent = headers.get("user-agent") or ""
        if user_agent and not fallback_user_agent:
            fallback_user_agent = user_agent
            fallback_url = url
        if not _is_grok_url(url):
            continue
        cookie = _sanitize_cookie(headers.get("cookie") or _cookie_header_from_array(request))
        if not cookie:
            continue
        user_agent = user_agent or fallback_user_agent
        score = _score_entry(url, cookie, user_agent)
        if best is None or score > best[0]:
            best = (score, cookie, user_agent, url)

    if best is None:
        raise HarImportError("未在 HAR 中找到 grok.com 请求 Cookie")
    _score, cookie, user_agent, source_url = best
    if not user_agent:
        raise HarImportError("未在 HAR 中找到 User-Agent")

    return HarImportResult(
        cookie=cookie,
        user_agent=user_agent.strip(),
        browser=browser_from_user_agent(user_agent),
        source_url=source_url or fallback_url,
    )


def browser_from_user_agent(user_agent: str) -> str:
    lower = user_agent.lower()
    edge = re.search(r"edg/(\d+)", lower)
    if edge:
        major = edge.group(1)
        # Only use edge fingerprint for exact version match (edge99/edge101)
        # For newer Edge (e.g. 147), fall back to chrome fingerprint
        exact_edge = _supported_browser(f"edge{major}")
        if exact_edge == f"edge{major}":
            return exact_edge
        exact_chrome = _supported_browser(f"chrome{major}")
        if exact_chrome == f"chrome{major}":
            return exact_chrome
        return _best_chrome_fallback() or f"chrome{major}"

    chrome = re.search(r"(?:chrome|chromium|crios)/(\d+)", lower)
    if chrome:
        major = chrome.group(1)
        suffix = "_android" if "android" in lower else ""
        exact = _supported_browser(f"chrome{major}{suffix}")
        if exact == f"chrome{major}{suffix}":
            return exact
        if suffix:
            exact_a = _supported_browser("chrome_android")
            if exact_a == "chrome_android":
                return exact_a
        return _best_chrome_fallback() or f"chrome{major}{suffix}"

    return _best_chrome_fallback() or "chrome146"


def _best_chrome_fallback() -> str:
    """返回 curl_cffi 支持的最新 Chrome 指纹。"""
    for candidate in ("chrome146", "chrome145", "chrome142", "chrome136", "chrome"):
        result = _supported_browser(candidate)
        if result:
            return result
    return ""


def _supported_browser(candidate: str) -> str:
    try:
        from curl_cffi.requests.impersonate import BrowserTypeLiteral

        supported = {str(item) for item in get_args(BrowserTypeLiteral)}
    except Exception:
        return candidate
    if candidate in supported:
        return candidate
    family = re.match(r"[a-z_]+", candidate)
    if family and family.group(0) in supported:
        return family.group(0)
    return ""


def _headers_map(headers: list[Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for header in headers:
        if not isinstance(header, dict):
            continue
        name = str(header.get("name") or "").strip().lower()
        value = str(header.get("value") or "").strip()
        if name and value:
            result[name] = value
    return result


def _cookie_header_from_array(request: dict[str, Any]) -> str:
    cookies = request.get("cookies") or []
    if not isinstance(cookies, list):
        return ""
    parts: list[str] = []
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if name:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _sanitize_cookie(cookie: str) -> str:
    cleaned = " ".join((cookie or "").replace("\r", " ").replace("\n", " ").split())
    return cleaned.rstrip(";")


def _is_grok_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "grok.com" or host.endswith(".grok.com")


def _score_entry(url: str, cookie: str, user_agent: str) -> int:
    path = (urlparse(url).path or "").lower()
    score = 10
    if "/rest/app-chat/conversations/new" in path:
        score += 100
    elif "/rest/app-chat" in path:
        score += 60
    elif path in {"", "/"}:
        score += 20
    if "sso=" in cookie:
        score += 15
    if "cf_clearance=" in cookie:
        score += 15
    if user_agent:
        score += 5
    return score
