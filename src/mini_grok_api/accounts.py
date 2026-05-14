"""多账号池 — Phase 1 实现。

设计要点：
- AccountPool 是全局单例，在 lifespan 初始化
- Phase 1 仅支持单账号（从 Settings 导入），预留多账号接口供 Phase 2 扩展
- acquire() 返回 AccountAcquisition context manager，退出时自动 mark_success
- 影子 settings（shadow）由 AccountPool 派生，替换 cookie/ua/browser/proxy/statsig_id
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Iterator

from .config import Settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class Account:
    """单个账号的身份信息。"""
    label: str                        # 账号标识，e.g. "default" / "account-2"
    grok_cookie: str
    grok_user_agent: str = ""
    grok_browser: str = ""
    grok_proxy: str = ""
    grok_statsig_id: str = ""

    # 运行时状态
    failure_count: int = 0
    success_count: int = 0
    last_failure_at: float = 0.0
    last_success_at: float = 0.0
    cooldown_until: float = 0.0       # Unix 时间戳，未冷却时不应被选中
    last_failure_code: str = ""


@dataclass
class AccountAcquisition:
    """acquire() 返回的 context，调用方通过 acq.settings 使用影子 settings。"""
    label: str
    account: Account
    settings: Settings                # 派生的影子 settings，已替换 cookie/ua/browser/proxy/statsig_id
    _pool: "AccountPool"
    _marked: bool = False             # 是否已手动 mark（防止 __exit__ 重复 mark）

    def mark_failure(self, code: str, retry_after: int | None = None) -> None:
        """显式标记失败。code 对应 GrokClientError.code。"""
        if not self._marked:
            self._marked = True
            self._pool.mark_failure(self.label, code, retry_after=retry_after)

    def mark_success(self) -> None:
        if not self._marked:
            self._marked = True
            self._pool.mark_success(self.label)


# ---------------------------------------------------------------------------
# AccountPool
# ---------------------------------------------------------------------------


class AccountPool:
    """账号池，Phase 1 单账号实现，接口兼容 Phase 2 多账号扩展。"""

    # 冷却策略（秒）：按 code 映射冷却时长
    _COOLDOWN_BY_CODE: dict[str, int] = {
        "image_rate_limited": 300,       # 5 分钟
        "upstream_unauthorized": 3600,   # 1 小时
        "cloudflare_challenge": 120,     # 2 分钟
        "upstream_5xx": 30,              # 30 秒
        "timeout": 10,                   # 10 秒
    }
    _DEFAULT_COOLDOWN = 30  # 未知 code 的默认冷却

    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}
        self._base_settings: Settings | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 账号管理
    # ------------------------------------------------------------------

    def import_from_settings(self, settings: Settings) -> None:
        """从当前 Settings 导入 default 账号（零账号 fallback）。

        如果已存在 default 账号则更新 cookie 等凭据（保留运行时统计）。
        """
        self._base_settings = settings
        with self._lock:
            label = "default"
            if label in self._accounts:
                # 更新凭据，保留统计
                acc = self._accounts[label]
                acc.grok_cookie = settings.grok_cookie
                acc.grok_user_agent = settings.grok_user_agent
                acc.grok_browser = settings.grok_browser
                acc.grok_proxy = settings.grok_proxy
                acc.grok_statsig_id = settings.grok_statsig_id
            else:
                self._accounts[label] = Account(
                    label=label,
                    grok_cookie=settings.grok_cookie,
                    grok_user_agent=settings.grok_user_agent,
                    grok_browser=settings.grok_browser,
                    grok_proxy=settings.grok_proxy,
                    grok_statsig_id=settings.grok_statsig_id,
                )
        logger.info("account pool: imported account %r (cookie_len=%d)", label, len(settings.grok_cookie))

    def add_account(self, label: str, account: Account) -> None:
        """添加或更新账号（Phase 2 admin API 用）。"""
        with self._lock:
            self._accounts[label] = account
        logger.info("account pool: added account %r", label)

    def remove_account(self, label: str) -> bool:
        with self._lock:
            if label in self._accounts:
                del self._accounts[label]
                logger.info("account pool: removed account %r", label)
                return True
            return False

    def list_accounts(self) -> list[dict]:
        """返回账号列表摘要（不含敏感凭据）。"""
        with self._lock:
            return [
                {
                    "label": acc.label,
                    "failure_count": acc.failure_count,
                    "success_count": acc.success_count,
                    "last_failure_code": acc.last_failure_code,
                    "cooldown_until": acc.cooldown_until,
                    "in_cooldown": time.time() < acc.cooldown_until,
                }
                for acc in self._accounts.values()
            ]

    # ------------------------------------------------------------------
    # acquire
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def acquire(
        self,
        model_id: str | None = None,
        force_label: str | None = None,
    ) -> Iterator[AccountAcquisition]:
        """选择账号，派生影子 settings，yield AccountAcquisition。

        正常退出（无异常）自动 mark_success；
        若调用方已显式调用 acq.mark_failure()，则不再重复 mark。
        """
        acq = self._select(model_id=model_id, force_label=force_label)
        try:
            yield acq
        finally:
            if not acq._marked:
                acq.mark_success()

    def _select(
        self,
        model_id: str | None = None,
        force_label: str | None = None,
    ) -> AccountAcquisition:
        """选出最优账号，返回 AccountAcquisition。"""
        with self._lock:
            accounts = list(self._accounts.values())

        if not accounts:
            raise RuntimeError("AccountPool: no accounts available")

        now = time.time()

        if force_label:
            acc = self._accounts.get(force_label)
            if acc is None:
                raise RuntimeError(f"AccountPool: forced account {force_label!r} not found")
        else:
            # Phase 1: 单账号，直接选；Phase 2 可改为按冷却状态 / 成功率选
            available = [a for a in accounts if now >= a.cooldown_until]
            if not available:
                # 所有账号都在冷却中，选冷却最早结束的
                acc = min(accounts, key=lambda a: a.cooldown_until)
                logger.warning(
                    "account pool: all accounts in cooldown, using %r (cooldown ends in %.0fs)",
                    acc.label, acc.cooldown_until - now,
                )
            else:
                # 选失败次数最少的（Phase 2 可加权）
                acc = min(available, key=lambda a: a.failure_count)

        shadow_settings = self._derive_settings(acc)
        return AccountAcquisition(
            label=acc.label,
            account=acc,
            settings=shadow_settings,
            _pool=self,
        )

    def _derive_settings(self, acc: Account) -> Settings:
        """从 base_settings 派生影子 settings，替换账号相关字段。"""
        base = self._base_settings
        if base is None:
            raise RuntimeError("AccountPool: base settings not initialized (call import_from_settings first)")
        return replace(
            base,
            grok_cookie=acc.grok_cookie or base.grok_cookie,
            grok_user_agent=acc.grok_user_agent or base.grok_user_agent,
            grok_browser=acc.grok_browser or base.grok_browser,
            grok_proxy=acc.grok_proxy if acc.grok_proxy is not None else base.grok_proxy,
            grok_statsig_id=acc.grok_statsig_id or base.grok_statsig_id,
        )

    # ------------------------------------------------------------------
    # 状态上报
    # ------------------------------------------------------------------

    def mark_success(self, label: str) -> None:
        with self._lock:
            acc = self._accounts.get(label)
            if acc is None:
                return
            acc.success_count += 1
            acc.last_success_at = time.time()
            # 成功后清空冷却（允许下次直接使用）
            if acc.cooldown_until > 0:
                acc.cooldown_until = 0.0
                acc.last_failure_code = ""

    def mark_failure(self, label: str, code: str, retry_after: int | None = None) -> None:
        """标记失败，进入冷却期。

        retry_after 为 None 时按 code 查默认冷却时长；
        显式传入则优先使用（上游 429 的 Retry-After header 值）。
        """
        with self._lock:
            acc = self._accounts.get(label)
            if acc is None:
                return
            acc.failure_count += 1
            acc.last_failure_at = time.time()
            acc.last_failure_code = code or ""
            cooldown = retry_after if retry_after is not None else self._COOLDOWN_BY_CODE.get(code, self._DEFAULT_COOLDOWN)
            acc.cooldown_until = time.time() + cooldown
        logger.warning(
            "account pool: account=%r code=%r failure_count=%d cooldown=%ds",
            label, code, acc.failure_count, cooldown,
        )
