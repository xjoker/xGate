"""运行状态统计。"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True, slots=True)
class MonitorSnapshot:
    total_requests: int
    success_count: int
    failure_count: int
    recent_upstream_status: int | None
    recent_error_summary: str
    cloudflare_challenge: bool


class Monitor:
    def __init__(self) -> None:
        self._lock = Lock()
        self._total_requests = 0
        self._success_count = 0
        self._failure_count = 0
        self._recent_upstream_status: int | None = None
        self._recent_error_summary = ""
        self._cloudflare_challenge = False

    def record_start(self) -> None:
        with self._lock:
            self._total_requests += 1

    def record_success(self, status: int = 200) -> None:
        with self._lock:
            self._success_count += 1
            self._recent_upstream_status = status
            self._recent_error_summary = ""

    def record_failure(self, status: int, summary: str, *, cloudflare: bool = False) -> None:
        with self._lock:
            self._failure_count += 1
            self._recent_upstream_status = status
            self._recent_error_summary = summary[:200]
            self._cloudflare_challenge = cloudflare

    def snapshot(self) -> MonitorSnapshot:
        with self._lock:
            return MonitorSnapshot(
                total_requests=self._total_requests,
                success_count=self._success_count,
                failure_count=self._failure_count,
                recent_upstream_status=self._recent_upstream_status,
                recent_error_summary=self._recent_error_summary,
                cloudflare_challenge=self._cloudflare_challenge,
            )
