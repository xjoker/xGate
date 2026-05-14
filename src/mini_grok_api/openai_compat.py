"""OpenAI Chat Completions 响应与错误格式。"""

from __future__ import annotations

import json
import logging
import secrets
import time
from functools import lru_cache
from typing import Any

import orjson

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_encoder():
    """加载 tiktoken 编码器（cl100k_base），失败时返回 None。"""
    try:
        import tiktoken
        return tiktoken.get_encoding("cl100k_base")
    except Exception as e:
        logger.warning("tiktoken unavailable, falling back to len/4: %s", e)
        return None


def count_tokens(text: str) -> int:
    """用 tiktoken 计算 token 数；tiktoken 不可用时退化为 len(text)//4。"""
    if not text:
        return 0
    enc = _get_encoder()
    if enc is None:
        return max(1, len(text) // 4)
    try:
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // 4)

# 静态 system_fingerprint：OpenAI 用它标识同一份模型权重 + 后端配置。
# 我们没有真正的版本指纹，给一个稳定常量足以让 SDK 解析通过。
SYSTEM_FINGERPRINT = "fp_xgate"


def response_id() -> str:
    return f"chatcmpl-{int(time.time() * 1000)}{secrets.token_hex(4)}"


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    return count_tokens(text)


def usage(prompt: str, completion: str) -> dict:
    prompt_tokens = estimate_tokens(prompt)
    completion_tokens = estimate_tokens(completion)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }


def chat_response(
    model: str,
    content: str,
    prompt: str,
    *,
    rid: str | None = None,
    finish_reason: str = "stop",
) -> dict:
    return {
        "id": rid or response_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "system_fingerprint": SYSTEM_FINGERPRINT,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
                "logprobs": None,
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
    role: str | None = "assistant",
) -> dict:
    delta: dict[str, Any] = {}
    if role is not None:
        delta["role"] = role
    delta["content"] = content
    choice: dict[str, Any] = {
        "index": 0,
        "delta": delta,
        "logprobs": None,
    }
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {
        "id": rid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "system_fingerprint": SYSTEM_FINGERPRINT,
        "choices": [choice],
    }


def stream_usage_chunk(rid: str, model: str, prompt: str, completion: str) -> dict:
    """流式末尾的 usage chunk（仅在 stream_options.include_usage=true 时发出）。"""
    return {
        "id": rid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "system_fingerprint": SYSTEM_FINGERPRINT,
        "choices": [],
        "usage": usage(prompt, completion),
    }


def sse_data(payload: dict) -> str:
    return f"data: {orjson.dumps(payload).decode('utf-8')}\n\n"


# 错误 status → OpenAI 标准 type 映射。
# 调用方仍可显式传 error_type 覆盖默认。
_STATUS_TO_TYPE = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    409: "invalid_request_error",
    422: "invalid_request_error",
    429: "rate_limit_error",
    500: "server_error",
    502: "api_error",
    503: "api_error",
    504: "api_error",
}


def type_for_status(status: int) -> str:
    if status in _STATUS_TO_TYPE:
        return _STATUS_TO_TYPE[status]
    if status >= 500:
        return "api_error"
    if status >= 400:
        return "invalid_request_error"
    return "server_error"


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


# ---------------------------------------------------------------------------
# Tool call 支持（基础版 / single-shot tool_call）
#
# 不支持的功能（follow-up 任务）：
# - 多轮 tool_call（tool 结果回填后续轮）
# - 流式 tool_call 增量（delta arguments 逐字符输出）
# - tool_choice 指定具体 function name 时的强约束（best-effort prompt 提示）
# ---------------------------------------------------------------------------

def parse_tool_call(text: str) -> dict | None:
    """尝试从模型输出中解析 tool_call JSON。

    格式要求：整段输出（strip 后）必须是 ``{"tool_call": {"name": ..., "arguments": ...}}``。
    解析失败或格式不符时返回 None，让调用方按普通 content 处理。

    基础版约束：只处理单个 tool_call；不支持多调用、嵌套等复杂结构。
    """
    stripped = text.strip()
    if not stripped.startswith('{"tool_call"'):
        return None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    tc = obj.get("tool_call")
    if not isinstance(tc, dict):
        return None
    name = tc.get("name")
    if not isinstance(name, str) or not name:
        return None
    return tc  # {"name": ..., "arguments": ...}


def tool_call_id() -> str:
    return f"call_{secrets.token_hex(12)}"


def chat_response_tool_call(
    model: str,
    tool_name: str,
    tool_arguments: Any,
    prompt: str,
    *,
    rid: str | None = None,
) -> dict:
    """构造 OpenAI 标准 tool_calls 响应（非流式）。

    ``tool_arguments`` 可以是 dict / list / str；若为 dict/list 会序列化为 JSON 字符串，
    符合 OpenAI 规范中 ``function.arguments`` 必须是 string 的要求。
    """
    if isinstance(tool_arguments, str):
        args_str = tool_arguments
    else:
        args_str = json.dumps(tool_arguments, ensure_ascii=False)

    content_str = json.dumps({"tool_call": {"name": tool_name, "arguments": tool_arguments}}, ensure_ascii=False)
    return {
        "id": rid or response_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "system_fingerprint": SYSTEM_FINGERPRINT,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tool_call_id(),
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": args_str,
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
                "logprobs": None,
            }
        ],
        "usage": usage(prompt, content_str),
    }
