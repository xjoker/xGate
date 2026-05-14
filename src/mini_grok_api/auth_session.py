"""HttpOnly Cookie 鉴权 session（SQLite 持久化）。

设计取舍：
- SQLite 持久化，进程重启后 TTL 内的 session 不失效。
- token = 32 字节随机 hex（无需 HMAC，验存在性即可）。
- 同时下发 CSRF token（可读 cookie，前端 JS 读出后回填到 X-CSRF-Token Header），
  采用 Double Submit Cookie 模式：服务端比对 cookie 与 header 是否一致即可。
- 过期时间 24h，惰性清理（create/size 时顺手清过期行）。
- 多连接安全：WAL 模式，check_same_thread=False，SQLite 自身处理并发写冲突。
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .db import DB_PATH as _DB_PATH

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
    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS, db_path: Path = _DB_PATH) -> None:
        self._ttl = ttl_seconds
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_table()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_table(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    token      TEXT PRIMARY KEY,
                    csrf       TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_exp ON sessions(expires_at)"
            )

    def create(self) -> Session:
        token = secrets.token_hex(32)
        csrf = secrets.token_hex(16)
        expires_at = time.time() + self._ttl
        sess = Session(token=token, csrf=csrf, expires_at=expires_at)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (token, csrf, expires_at) VALUES (?, ?, ?)",
                (token, csrf, expires_at),
            )
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),))
        return sess

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT token, csrf, expires_at FROM sessions"
                " WHERE token=? AND expires_at > ?",
                (token, now),
            ).fetchone()
            if row is None:
                return None
            return Session(token=row["token"], csrf=row["csrf"], expires_at=row["expires_at"])

    def revoke(self, token: str | None) -> bool:
        if not token:
            return False
        with self._connect() as conn:
            r = conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            return r.rowcount > 0

    def revoke_all(self) -> int:
        """清空所有 session。api_key 轮换时调用，强制所有浏览器重新登录。"""
        with self._connect() as conn:
            r = conn.execute("DELETE FROM sessions")
            return r.rowcount

    def size(self) -> int:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),))
            return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]


# 全局单例
session_store = SessionStore()
