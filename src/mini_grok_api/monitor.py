"""运行状态统计 — 按 account_label 分桶，同时提供全局聚合视图。"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock


@dataclass(slots=True)
class AccountMetrics:
    total_requests: int = 0
    success_count: int = 0
    failure_count: int = 0
    recent_upstream_status: int | None = None
    recent_error_summary: str = ""


@dataclass(frozen=True, slots=True)
class MonitorSnapshot:
    # 全局聚合（兼容现有 admin/status 响应）
    total_requests: int
    success_count: int
    failure_count: int
    recent_upstream_status: int | None
    recent_error_summary: str
    cloudflare_challenge: bool
    # 新增：per-account 明细  label -> AccountMetrics
    per_account: dict[str, AccountMetrics]


class Monitor:
    def __init__(self) -> None:
        self._lock = Lock()
        self._buckets: dict[str, AccountMetrics] = {}
        self._cloudflare_challenge = False  # 全局 flag

    def _bucket(self, label: str) -> AccountMetrics:
        """内部：取或创建 bucket（需在 _lock 内调用）。"""
        if label not in self._buckets:
            self._buckets[label] = AccountMetrics()
        return self._buckets[label]

    def record_start(self, account_label: str = "") -> None:
        with self._lock:
            self._bucket(account_label).total_requests += 1

    def record_success(self, account_label: str = "", status: int = 200) -> None:
        with self._lock:
            b = self._bucket(account_label)
            b.success_count += 1
            b.recent_upstream_status = status
            b.recent_error_summary = ""

    def record_failure(
        self,
        account_label: str = "",
        status: int = 500,
        summary: str = "",
        *,
        cloudflare: bool = False,
    ) -> None:
        with self._lock:
            b = self._bucket(account_label)
            b.failure_count += 1
            b.recent_upstream_status = status
            b.recent_error_summary = summary[:200]
            if cloudflare:
                self._cloudflare_challenge = True

    def snapshot(self) -> MonitorSnapshot:
        with self._lock:
            total = sum(b.total_requests for b in self._buckets.values())
            success = sum(b.success_count for b in self._buckets.values())
            failure = sum(b.failure_count for b in self._buckets.values())

            # 全局 recent：取最近有错误的 bucket，否则取有状态的
            recent_status: int | None = None
            recent_err = ""
            for b in self._buckets.values():
                if b.recent_error_summary:
                    recent_status = b.recent_upstream_status
                    recent_err = b.recent_error_summary
                    break
            if not recent_err:
                for b in self._buckets.values():
                    if b.recent_upstream_status is not None:
                        recent_status = b.recent_upstream_status
                        break

            # 深拷贝 per_account dict（避免持有内部可变状态引用）
            per_account = {
                label: AccountMetrics(
                    total_requests=b.total_requests,
                    success_count=b.success_count,
                    failure_count=b.failure_count,
                    recent_upstream_status=b.recent_upstream_status,
                    recent_error_summary=b.recent_error_summary,
                )
                for label, b in self._buckets.items()
            }

            return MonitorSnapshot(
                total_requests=total,
                success_count=success,
                failure_count=failure,
                recent_upstream_status=recent_status,
                recent_error_summary=recent_err,
                cloudflare_challenge=self._cloudflare_challenge,
                per_account=per_account,
            )
