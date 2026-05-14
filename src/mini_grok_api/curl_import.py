"""解析 Chrome/Edge “复制为 cURL” 文本。"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from urllib.parse import urlparse

from .har_import import browser_from_user_agent


class CurlImportError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CurlImportResult:
    cookie: str
    user_agent: str
    browser: str
    url: str
    headers: dict[str, str]
    body: str
    statsig_id: str = ""


def parse_grok_curl(command: str, *, max_chars: int = 256_000) -> CurlImportResult:
    text = command.strip().replace("\\\r\n", " ").replace("\\\n", " ")
    if len(text) > max_chars:
        raise CurlImportError("cURL 文本过长，最大支持 256 KiB")
    if not text:
        raise CurlImportError("cURL 文本不能为空")

    try:
        args = shlex.split(text)
    except ValueError as exc:
        raise CurlImportError("无法解析 cURL，请确认是 Chrome 或 Edge 复制出的命令") from exc

    if not args or args[0] != "curl":
        raise CurlImportError("请输入以 curl 开头的命令")

    url = ""
    headers: dict[str, str] = {}
    cookie = ""
    body = ""
    index = 1
    while index < len(args):
        arg = args[index]
        if arg in {"--header", "-H"}:
            value = _next_value(args, index, arg)
            name, header_value = _split_header(value)
            if name:
                headers[name.lower()] = header_value
            index += 2
            continue
        if arg.startswith("--header="):
            name, header_value = _split_header(arg.split("=", 1)[1])
            if name:
                headers[name.lower()] = header_value
            index += 1
            continue
        if arg in {"--cookie", "-b"}:
            cookie = _next_value(args, index, arg).strip()
            index += 2
            continue
        if arg.startswith("--cookie="):
            cookie = arg.split("=", 1)[1].strip()
            index += 1
            continue
        if arg in {"--data", "--data-raw", "--data-binary", "--data-ascii", "-d"}:
            body = _next_value(args, index, arg)
            index += 2
            continue
        if any(arg.startswith(prefix) for prefix in ("--data=", "--data-raw=", "--data-binary=")):
            body = arg.split("=", 1)[1]
            index += 1
            continue
        if arg.startswith("-"):
            index += 1
            continue
        if not url:
            url = arg
        index += 1

    cookie = _sanitize_cookie(headers.get("cookie") or cookie)
    user_agent = (headers.get("user-agent") or "").strip()
    statsig_id = (headers.get("x-statsig-id") or "").strip()
    if not _is_grok_url(url):
        raise CurlImportError("cURL URL 必须是 grok.com")
    if not cookie:
        raise CurlImportError("未在 cURL 中找到 Cookie，请使用包含 Cookie 的浏览器请求")
    if not user_agent:
        raise CurlImportError("未在 cURL 中找到 User-Agent")

    return CurlImportResult(
        cookie=cookie,
        user_agent=user_agent,
        browser=browser_from_user_agent(user_agent),
        url=url,
        headers=headers,
        body=body,
        statsig_id=statsig_id,
    )


def _next_value(args: list[str], index: int, name: str) -> str:
    if index + 1 >= len(args):
        raise CurlImportError(f"{name} 缺少参数值")
    return args[index + 1]


def _split_header(value: str) -> tuple[str, str]:
    if ":" not in value:
        return "", ""
    name, header_value = value.split(":", 1)
    return name.strip(), header_value.strip()


def _sanitize_cookie(cookie: str) -> str:
    return " ".join((cookie or "").replace("\r", " ").replace("\n", " ").split()).rstrip(";")


def _is_grok_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "grok.com" or host.endswith(".grok.com")
