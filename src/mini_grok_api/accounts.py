"""多账号池管理。

此文件由 Agent A 实现核心逻辑。Agent C 在此处提供最小可运行 stub，
供 admin endpoints 和集成测试使用。Agent A 合并后会替换此文件的实现。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from threading import RLock
from typing import Any


@dataclass
class Account:
    """单个 Grok 账号的完整配置与状态。"""

    label: str
    cookie: str
    user_agent: str = ""
    browser: str = "chrome142"
    proxy: str = ""
    statsig_id: str = ""
    enabled: bool = True
    priority: int = 1
    weight: int = 10

    # 运行时状态（不持久化到 TOML，但可在 list_accounts 中体现）
    status: str = "enabled"          # enabled | manually_disabled | auto_disabled | cooling
    cooldown_until: float = 0.0
    last_used_at: float = 0.0
    last_error_code: str = ""
    last_error_at: float = 0.0
    consecutive_failures: int = 0
    success_count: int = 0
    fail_count: int = 0


@dataclass(frozen=True)
class AccountInfo:
    """只读视图，用于列表展示。"""

    label: str
    enabled: bool
    priority: int
    weight: int
    status: str
    cooldown_until: float
    last_used_at: float
    last_error_code: str
    last_error_at: float
    consecutive_failures: int
    success_count: int
    fail_count: int
    cookie_masked: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "enabled": self.enabled,
            "priority": self.priority,
            "weight": self.weight,
            "status": self.status,
            "cooldown_until": self.cooldown_until,
            "last_used_at": self.last_used_at,
            "last_error_code": self.last_error_code,
            "last_error_at": self.last_error_at,
            "consecutive_failures": self.consecutive_failures,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "cookie_masked": self.cookie_masked,
        }


def _mask_cookie(cookie: str, *, keep: int = 8) -> str:
    text = (cookie or "").strip()
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "***"
    return f"{text[:keep]}...{text[-keep:]}"


class AccountPool:
    """线程安全的账号池。

    数据存储在内存中（dict[label, Account]），由 main.py 的 lifespan
    负责从 mini.toml 初始化，并在账号变更时持久化。

    Agent A 将补充：
    - 加权轮询 acquire() / release()
    - 冷却期、失败计数、自动禁用逻辑
    - TOML 持久化（upsert_account / delete_account 后需落盘）
    """

    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}
        self._lock = RLock()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_accounts(self) -> list[AccountInfo]:
        """返回所有账号的只读快照列表，按 priority asc / label asc 排序。"""
        with self._lock:
            accounts = list(self._accounts.values())
        accounts.sort(key=lambda a: (a.priority, a.label))
        return [self._to_info(a) for a in accounts]

    def get_account(self, label: str) -> Account | None:
        """按 label 查询账号，未找到返回 None。"""
        with self._lock:
            return self._accounts.get(label)

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def upsert_account(self, account: Account) -> None:
        """新增或更新账号（label 为主键）。"""
        with self._lock:
            existing = self._accounts.get(account.label)
            if existing is not None:
                # 保留运行时状态字段
                updated = Account(
                    label=account.label,
                    cookie=account.cookie,
                    user_agent=account.user_agent,
                    browser=account.browser,
                    proxy=account.proxy,
                    statsig_id=account.statsig_id,
                    enabled=account.enabled,
                    priority=account.priority,
                    weight=account.weight,
                    status=existing.status if account.enabled == existing.enabled else (
                        "enabled" if account.enabled else "manually_disabled"
                    ),
                    cooldown_until=existing.cooldown_until,
                    last_used_at=existing.last_used_at,
                    last_error_code=existing.last_error_code,
                    last_error_at=existing.last_error_at,
                    consecutive_failures=existing.consecutive_failures,
                    success_count=existing.success_count,
                    fail_count=existing.fail_count,
                )
                self._accounts[account.label] = updated
            else:
                account.status = "enabled" if account.enabled else "manually_disabled"
                self._accounts[account.label] = account

    def delete_account(self, label: str) -> bool:
        """删除账号，返回是否成功（账号不存在返回 False）。"""
        with self._lock:
            if label not in self._accounts:
                return False
            del self._accounts[label]
            return True

    def set_enabled(self, label: str, enabled: bool) -> bool:
        """启用或禁用账号，返回是否成功（账号不存在返回 False）。"""
        with self._lock:
            acc = self._accounts.get(label)
            if acc is None:
                return False
            acc.enabled = enabled
            acc.status = "enabled" if enabled else "manually_disabled"
            self._accounts[label] = acc
            return True

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _to_info(self, acc: Account) -> AccountInfo:
        return AccountInfo(
            label=acc.label,
            enabled=acc.enabled,
            priority=acc.priority,
            weight=acc.weight,
            status=acc.status,
            cooldown_until=acc.cooldown_until,
            last_used_at=acc.last_used_at,
            last_error_code=acc.last_error_code,
            last_error_at=acc.last_error_at,
            consecutive_failures=acc.consecutive_failures,
            success_count=acc.success_count,
            fail_count=acc.fail_count,
            cookie_masked=_mask_cookie(acc.cookie),
        )


# 模块级全局实例，由 main.py lifespan 初始化
account_pool = AccountPool()
