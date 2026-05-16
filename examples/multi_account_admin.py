"""多账号管理 — 用 admin API 批量自动化。

适用场景：
- 启动脚本里批量导入预备账号
- 监控 + 自动 disable 长期失效账号
- 查 quota 排序找最闲的账号
"""

from __future__ import annotations

import json
import os
import sys

import requests

API_KEY = os.environ["XGATE_API_KEY"]
BASE = os.environ.get("XGATE_BASE_URL", "http://127.0.0.1:8024")
H = {"Authorization": f"Bearer {API_KEY}"}


def list_accounts() -> list[dict]:
    return requests.get(f"{BASE}/admin/accounts", headers=H).json()["accounts"]


def import_curl(curl_text: str, label: str, priority: int = 5, weight: int = 10) -> dict:
    """从 cURL 一键导入新账号（不动 settings 即不污染 default）。"""
    return requests.post(
        f"{BASE}/admin/accounts/import-curl",
        headers=H,
        json={"curl": curl_text, "label": label, "priority": priority, "weight": weight},
    ).json()


def toggle_enabled(label: str, enabled: bool) -> dict:
    return requests.post(
        f"{BASE}/admin/accounts/{label}/enabled",
        headers=H,
        json={"enabled": enabled},
    ).json()


def delete(label: str) -> dict:
    return requests.delete(f"{BASE}/admin/accounts/{label}", headers=H).json()


def edit(label: str, *, priority: int | None = None, weight: int | None = None,
         cookie: str | None = None) -> dict:
    """部分编辑。

    cookie=None → 留空字符串发请求，xGate 后端 (v0.3.x+) 会**保留旧值**。
    cookie="新的 cookie 字符串" → 覆盖旧值。
    注意：千万别传 `cookie=""` 期望清空，后端不会清；如果你真的想"清空 cookie"
    （让账号不可用），请用 `toggle_enabled(label, False)` 替代。
    """
    payload: dict = {"label": label, "cookie": cookie or ""}
    if priority is not None:
        payload["priority"] = priority
    if weight is not None:
        payload["weight"] = weight
    return requests.post(f"{BASE}/admin/accounts", headers=H, json=payload).json()


def quota_per_account() -> dict[str, dict]:
    """返回每账号 × 模型的额度快照（来自后台 poll cache，零额外上游请求）。"""
    return requests.post(f"{BASE}/admin/dashboard", headers=H).json().get("per_account", {})


def find_idlest_account(model_id: str = "grok-4.20-fast") -> str | None:
    """选 fast 模型剩余 % 最高的账号（用于 sticky binding 等手动分发）。"""
    pa = quota_per_account()
    best_label = None
    best_pct = -1.0
    for label, models in pa.items():
        for m in models:
            if m["model_id"] == model_id and m["total"] > 0:
                pct = m["remaining"] / m["total"]
                if pct > best_pct:
                    best_pct = pct
                    best_label = label
    return best_label


if __name__ == "__main__":
    print("=== 当前账号池 ===")
    for a in list_accounts():
        print(f"  {a['label']:15s} prio={a['priority']} status={a['status']:18s} "
              f"cookie={a['cookie_masked']}")
    print()
    idlest = find_idlest_account()
    print(f"=== 当前 fast 模型最闲账号: {idlest or '(none)'} ===")
    if idlest:
        print(f"  → 你可以用 X-Account-Label: {idlest} 把请求路过去")
