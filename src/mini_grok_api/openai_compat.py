"""OpenAI Chat Completions 响应与错误格式。"""

from __future__ import annotations

import secrets
import time
from typing import Any

import orjson


def response_id() -> str:
    return f"chatcmpl-{int(time.time() * 1000)}{secrets.token_hex(4)}"


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def usage(prompt: str, completion: str) -> dict:
    prompt_tokens = estimate_tokens(prompt)
    completion_tokens = estimate_tokens(completion)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def chat_response(model: str, content: str, prompt: str, *, rid: str | None = None) -> dict:
    return {
        "id": rid or response_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage(prompt, content),
    }


def stream_chunk(
    rid: str,
    model: str,
    content: str,
    *,
    finish_reason: str | None = None,
) -> dict:
    choice: dict[str, Any] = {
        "index": 0,
        "delta": {"role": "assistant", "content": content},
    }
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {
        "id": rid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [choice],
    }


def sse_data(payload: dict) -> str:
    return f"data: {orjson.dumps(payload).decode('utf-8')}\n\n"


def error_payload(
    message: str,
    *,
    error_type: str = "server_error",
    code: str | None = None,
    param: str | None = None,
) -> dict:
    return {
        "error": {
            "message": message,
            "type": error_type,
            "code": code,
            "param": param,
        }
    }


def sse_error(message: str, *, error_type: str = "server_error", code: str | None = None) -> str:
    payload = error_payload(message, error_type=error_type, code=code)
    return f"event: error\ndata: {orjson.dumps(payload).decode('utf-8')}\n\n"
