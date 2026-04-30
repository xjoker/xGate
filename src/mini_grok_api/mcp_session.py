"""MCP session ↔ Grok conversation 状态映射（内存存储）。

每次 grok_chat 工具调用结束时，把 (mcp_session_id, conversation_id, response_id) upsert。
下次同一 MCP session 调用时自动续轮，无需客户端显式传 conversation_id。
"""
from __future__ import annotations

import time
from threading import RLock

_lock = RLock()
_sessions: dict[str, dict[str, str | float]] = {}


def upsert(mcp_session_id: str, conversation_id: str, last_response_id: str) -> None:
    if not mcp_session_id:
        return
    with _lock:
        _sessions[mcp_session_id] = {
            "conversation_id": conversation_id,
            "last_response_id": last_response_id,
            "updated_at": time.time(),
        }


def get(mcp_session_id: str) -> tuple[str | None, str | None]:
    """返回 (conversation_id, last_response_id)，未找到时返回 (None, None)。"""
    if not mcp_session_id:
        return None, None
    with _lock:
        s = _sessions.get(mcp_session_id)
    if not s:
        return None, None
    return str(s["conversation_id"]), str(s["last_response_id"])


def cleanup(max_age_seconds: float = 7 * 86400) -> int:
    """清理超过 max_age_seconds 的旧 session，返回清理数量。"""
    cutoff = time.time() - max_age_seconds
    with _lock:
        stale = [k for k, v in _sessions.items() if float(v.get("updated_at", 0)) < cutoff]
        for k in stale:
            del _sessions[k]
    return len(stale)
