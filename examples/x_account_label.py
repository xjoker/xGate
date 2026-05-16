"""X-Account-Label header — 客户端强制指定走某个账号 (v0.3.1+)。

适用场景：
- debug：明确测试 default 账号的额度是否正常
- A/B：同 prompt 跑两个账号看返回差异
- 高优任务隔离：把生产请求固定走 priority=1 账号

错误响应：
- 账号不存在 → 400 `account_label_not_found`
- 账号被禁用 → 400 `account_label_disabled`

覆盖端点（9 个）：
  POST /v1/chat/completions
  POST /v1/videos/generate
  POST /v1/videos/status
  POST /v1/quota
  POST /v1/quota/chat
  POST /v1/quota/image
  POST /v1/images/chat-imagine
  POST /v1/images/generations
  POST /v1/images/stream/start
"""

from __future__ import annotations

import os

import requests

API_KEY = os.environ["XGATE_API_KEY"]
BASE = os.environ.get("XGATE_BASE_URL", "http://127.0.0.1:8024")


def quota_for(label: str) -> dict:
    """获取指定账号的 auto/fast 模式额度（不消耗任何配额）。"""
    r = requests.post(
        f"{BASE}/v1/quota",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "X-Account-Label": label,
        },
    )
    r.raise_for_status()
    return r.json().get("quotas", {})


def chat_with_label(label: str, prompt: str) -> str:
    r = requests.post(
        f"{BASE}/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "X-Account-Label": label,
            "Content-Type": "application/json",
        },
        json={
            "model": "grok-4.20-fast",
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


if __name__ == "__main__":
    # 查看 default 账号的额度
    q = quota_for("default")
    auto = q.get("auto", {})
    fast = q.get("fast", {})
    print(f"[default] auto={auto.get('remaining')}/{auto.get('total')} "
          f"fast={fast.get('remaining')}/{fast.get('total')}")

    # 强制 default 回答（即使 LRU 会选别的）
    reply = chat_with_label("default", "用一个词形容今天")
    print(f"[default reply] {reply}")

    # 错误演示：不存在的 label
    r = requests.post(
        f"{BASE}/v1/quota",
        headers={"Authorization": f"Bearer {API_KEY}", "X-Account-Label": "ghost"},
    )
    print(f"[ghost] status={r.status_code} body={r.json()}")
