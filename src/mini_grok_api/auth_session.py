"""HttpOnly Cookie 鉴权 session。

设计取舍：
- 内存 dict 存 token，进程重启即失效（可接受：本服务是单实例小工具）。
- token = 32 字节随机 hex（无需 HMAC，因为我们只验存在性，没有外部签发方）。
- 同时下发 CSRF token（可读 cookie，前端 JS 读出后回填到 X-CSRF-Token Header），
  采用 Double Submit Cookie 模式：服务端比对 cookie 与 header 是否一致即可。
- 过期时间 24h，惰性清理（每次校验时顺手清过期项）。

线程安全：单 RLock 覆盖所有读写。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from threading import RLock

SESSION_COOKIE = "xgate_session"
CSRF_COOKIE = "xgate_csrf"
CSRF_HEADER = "x-csrf-token"
DEFAULT_TTL_SECONDS = 24 * 3600


@dataclass(frozen=True, slots=True)
class Session:
    token: str
    csrf: str
    expires_at: float


class SessionStore:
    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._sessions: dict[str, Session] = {}
        self._lock = RLock()

    def create(self) -> Session:
        token = secrets.token_hex(32)
        csrf = secrets.token_hex(16)
        sess = Session(token=token, csrf=csrf, expires_at=time.time() + self._ttl)
        with self._lock:
            self._sessions[token] = sess
            self._gc_locked()
        return sess

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        with self._lock:
            sess = self._sessions.get(token)
            if sess is None:
                return None
            if sess.expires_at <= time.time():
                self._sessions.pop(token, None)
                return None
            return sess

    def revoke(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            return self._sessions.pop(token, None) is not None

    def revoke_all(self) -> int:
        """清空所有 session。api_key 轮换时调用，强制所有浏览器重新登录。"""
        with self._lock:
            n = len(self._sessions)
            self._sessions.clear()
            return n

    def _gc_locked(self) -> None:
        now = time.time()
        expired = [k for k, v in self._sessions.items() if v.expires_at <= now]
        for k in expired:
            self._sessions.pop(k, None)

    def size(self) -> int:
        with self._lock:
            self._gc_locked()
            return len(self._sessions)


# 全局单例
session_store = SessionStore()
