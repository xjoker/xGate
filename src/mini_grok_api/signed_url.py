"""HMAC 签名 URL 工具 — 为本地文件 endpoint 提供时效性签名保护。

格式：?sig=<hex>&exp=<unix_ts>
算法：HMAC-SHA256(api_key, "{path}|{exp}")
TTL：默认 1 小时（可通过 ttl_seconds 调整）

使用场景：
  签发：sign_file_url("/v1/files/image/<sid>/<fn>")
  校验：在 endpoint 中调用 verify_signed_path(request.url.path, sig, exp, api_key)
"""

from __future__ import annotations

import hashlib
import hmac
import time

# 默认签名有效期（秒）
_DEFAULT_TTL = 3600


def sign_path(path: str, api_key: str, ttl_seconds: int = _DEFAULT_TTL) -> str:
    """对文件路径签名，返回完整 query string，如 'sig=abc&exp=1234'。

    Args:
        path: URL path 部分，如 /v1/files/image/<session_id>/<filename>，不含 query
        api_key: 服务端 API Key（与配置中的 auth.api_key 一致）
        ttl_seconds: 签名有效期（秒），默认 3600（1 小时）

    Returns:
        query string，如 "sig=abc123&exp=1714000000"
    """
    exp = int(time.time()) + ttl_seconds
    payload = f"{path}|{exp}"
    sig = hmac.new(
        api_key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"sig={sig}&exp={exp}"


def verify_signed_path(path: str, sig: str, exp: int, api_key: str) -> bool:
    """验签 + 过期检查。

    Args:
        path: URL path 部分（不含 query），须与签名时一致
        sig: 客户端传来的 sig 参数（hex 字符串）
        exp: 客户端传来的 exp 参数（Unix 时间戳）
        api_key: 服务端 API Key

    Returns:
        True 表示签名合法且未过期；False 表示任一条件失败
    """
    # 先检查过期，避免无意义的 HMAC 计算（防时序攻击仍用 compare_digest）
    if int(time.time()) > exp:
        return False
    payload = f"{path}|{exp}"
    expected = hmac.new(
        api_key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    # compare_digest 防止时序攻击
    try:
        return hmac.compare_digest(expected, sig)
    except (TypeError, ValueError):
        return False


def sign_file_url(path: str, api_key: str, ttl_seconds: int = _DEFAULT_TTL) -> str:
    """返回带签名 query string 的完整路径，便于一行式调用。

    示例：
        url = sign_file_url("/v1/files/image/abc/foo.jpg", settings.api_key)
        # → "/v1/files/image/abc/foo.jpg?sig=...&exp=..."

    Args:
        path: URL path 部分，不含 query
        api_key: 服务端 API Key
        ttl_seconds: 签名有效期（秒），默认 3600

    Returns:
        path + "?" + query string
    """
    return f"{path}?{sign_path(path, api_key, ttl_seconds)}"
