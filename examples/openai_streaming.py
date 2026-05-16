"""流式 chat completion — 演示 SSE 增量解析。

xGate 完全实现 OpenAI 流式契约：
- 每个 delta chunk 含 `choices[0].delta.content` 增量
- 最后一个 chunk `choices[0].finish_reason` 不为 None
- stream_options.include_usage=True 时会在 `[DONE]` 前发出一个含 usage 的 chunk
"""

from __future__ import annotations

import os

from openai import OpenAI

client = OpenAI(
    api_key=os.environ["XGATE_API_KEY"],
    base_url=os.environ.get("XGATE_BASE_URL", "http://127.0.0.1:8024/v1"),
)

stream = client.chat.completions.create(
    model="grok-4.20-fast",
    messages=[{"role": "user", "content": "用三段话讲一个程序员的小故事"}],
    stream=True,
    stream_options={"include_usage": True},  # 末尾返回 token 用量
)

print("streaming:")
for chunk in stream:
    if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
    if chunk.usage:
        print(f"\n\n[usage] prompt={chunk.usage.prompt_tokens} "
              f"completion={chunk.usage.completion_tokens} "
              f"total={chunk.usage.total_tokens}")
