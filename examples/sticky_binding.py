"""conversation_id sticky binding — 多轮对话固定走同一账号 (v0.3.2+)。

xGate 在 chat completions 请求里抽取 conversation_id（优先级）：
1. metadata.conversation_id（OpenAI 客户端常用）
2. metadata.conversationId（camelCase 兼容）
3. user 字段（OpenAI 标准 end-user 标识，部分客户端复用）

首次请求选号后写入 SQLite `conversation_account_map`（TTL 7d），后续同 conv_id
请求自动复用该账号 —— 避免 LRU 切号导致 Grok 上下文丢失。

选号优先级：X-Account-Label > sticky binding > LRU + soft_cooldown
绑定账号失效（被禁用/删除）→ 静默回退 LRU 并自动清理 binding (v0.3.6 BUG-G)
"""

from __future__ import annotations

import os
import uuid

import requests

API_KEY = os.environ["XGATE_API_KEY"]
BASE = os.environ.get("XGATE_BASE_URL", "http://127.0.0.1:8024")


def chat(messages: list[dict], conversation_id: str) -> str:
    r = requests.post(
        f"{BASE}/v1/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "model": "grok-4.20-fast",
            "messages": messages,
            "metadata": {"conversation_id": conversation_id},
        },
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def show_bindings() -> None:
    """调试：查看所有 sticky binding。"""
    r = requests.get(
        f"{BASE}/admin/conversation-bindings?limit=10",
        headers={"Authorization": f"Bearer {API_KEY}"},
    )
    print(f"[bindings ttl={r.json()['ttl_seconds']}s]")
    for b in r.json()["bindings"]:
        print(f"  {b['conversation_id']:50s} → {b['account_label']}")


if __name__ == "__main__":
    conv = f"demo-{uuid.uuid4().hex[:8]}"
    print(f"conv_id = {conv}\n")

    messages: list[dict] = []
    for prompt in ["我叫 Alice，你呢？", "我刚才说我叫什么？"]:
        messages.append({"role": "user", "content": prompt})
        reply = chat(messages, conv)
        print(f"[user] {prompt}")
        print(f"[ai]   {reply}\n")
        messages.append({"role": "assistant", "content": reply})

    show_bindings()
    print("\n注：两轮请求都走的是同一个账号（看 server log 的 sticky binding hit 行）。")
    print("如果没传 conversation_id，LRU 可能会在中途切账号，AI 就会忘记第一轮内容。")
