"""图片生成任务队列 — 支持优先级、暂停、恢复、取消、重排。"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from typing import TYPE_CHECKING

from .grok_client import GrokClientError, _init_session, resolve_aspect_ratio

if TYPE_CHECKING:
    from .db import LogDB
    from .ws_gateway import WsGateway

logger = logging.getLogger(__name__)

TaskStatus = Literal["pending", "running", "paused", "done", "cancelled", "failed"]


@dataclass
class ImageTask:
    id: str
    prompt: str
    target_count: int
    aspect_ratio: str
    enable_pro: bool
    interval_seconds: float
    status: TaskStatus
    priority: int          # 越小越先执行
    session_id: str
    generated_count: int = 0
    failed_count: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "target_count": self.target_count,
            "generated_count": self.generated_count,
            "failed_count": self.failed_count,
            "aspect_ratio": self.aspect_ratio,
            "enable_pro": self.enable_pro,
            "interval_seconds": self.interval_seconds,
            "status": self.status,
            "priority": self.priority,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


class TaskQueue:
    def __init__(self) -> None:
        self._tasks: dict[str, ImageTask] = {}
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._worker_task: asyncio.Task | None = None
        self._current_stop: asyncio.Event | None = None
        self._gateway: "WsGateway | None" = None
        self._log_db: "LogDB | None" = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_worker(self, gateway: "WsGateway", log_db: "LogDB | None" = None) -> None:
        self._gateway = gateway
        self._log_db = log_db
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._worker_loop(), name="task-queue-worker"
            )

    def stop_worker(self) -> None:
        if self._current_stop:
            self._current_stop.set()
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def add_task(
        self,
        prompt: str,
        target_count: int,
        *,
        aspect_ratio: str = "1:1",
        enable_pro: bool = False,
        interval_seconds: float = 5.0,
    ) -> ImageTask:
        async with self._lock:
            # 优先级 = 当前最大优先级 + 1（追加到末尾）
            priority = max((t.priority for t in self._tasks.values()), default=-1) + 1
            task = ImageTask(
                id=str(uuid.uuid4()),
                prompt=prompt,
                target_count=target_count,
                aspect_ratio=aspect_ratio,
                enable_pro=enable_pro,
                interval_seconds=interval_seconds,
                status="pending",
                priority=priority,
                session_id=str(uuid.uuid4()),
            )
            self._tasks[task.id] = task
            self._wake.set()
            return task

    async def pause_task(self, task_id: str) -> bool:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            if task.status == "running":
                task.status = "paused"
                if self._current_stop:
                    self._current_stop.set()
                return True
            if task.status == "pending":
                task.status = "paused"
                return True
            return False

    async def resume_task(self, task_id: str) -> bool:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "paused":
                return False
            task.status = "pending"
            self._wake.set()
            return True

    async def cancel_task(self, task_id: str) -> bool:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            if task.status in ("done", "cancelled", "failed"):
                return False
            was_running = task.status == "running"
            task.status = "cancelled"
            task.finished_at = time.time()
            if was_running and self._current_stop:
                self._current_stop.set()
            return True

    async def remove_task(self, task_id: str) -> bool:
        await self.cancel_task(task_id)
        async with self._lock:
            if task_id in self._tasks:
                del self._tasks[task_id]
                return True
            return False

    async def move_task(self, task_id: str, direction: Literal["up", "down"]) -> bool:
        """按优先级顺序上移或下移任务。"""
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            sorted_tasks = sorted(self._tasks.values(), key=lambda t: t.priority)
            idx = next((i for i, t in enumerate(sorted_tasks) if t.id == task_id), -1)
            if idx == -1:
                return False
            if direction == "up" and idx > 0:
                neighbor = sorted_tasks[idx - 1]
                task.priority, neighbor.priority = neighbor.priority, task.priority
                return True
            if direction == "down" and idx < len(sorted_tasks) - 1:
                neighbor = sorted_tasks[idx + 1]
                task.priority, neighbor.priority = neighbor.priority, task.priority
                return True
            return False

    def list_tasks(self) -> list[dict[str, Any]]:
        return [
            t.to_dict()
            for t in sorted(self._tasks.values(), key=lambda t: t.priority)
        ]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        t = self._tasks.get(task_id)
        return t.to_dict() if t else None

    def stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for t in self._tasks.values():
            counts[t.status] = counts.get(t.status, 0) + 1
        return {
            "total": len(self._tasks),
            "by_status": counts,
            "worker_running": self._worker_task is not None and not self._worker_task.done(),
        }

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    async def _worker_loop(self) -> None:
        logger.info("task queue worker started")
        while True:
            task = await self._pick_next()
            if task is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            await self._run_task(task)

    async def _pick_next(self) -> ImageTask | None:
        async with self._lock:
            for task in sorted(self._tasks.values(), key=lambda t: t.priority):
                if task.status == "pending":
                    return task
            return None

    async def _run_task(self, task: ImageTask) -> None:
        assert self._gateway is not None, "TaskQueue.start_worker() not called"
        stop_event = asyncio.Event()

        async with self._lock:
            # 重新检查：可能在等待期间被暂停/取消
            if task.status != "pending":
                return
            task.status = "running"
            task.started_at = task.started_at or time.time()
            self._current_stop = stop_event

        logger.info(
            "task queue: running task=%s prompt=%r target=%d generated=%d",
            task.id[:8], task.prompt[:40], task.target_count, task.generated_count,
        )

        session_dir = _init_session(task.session_id, prompt=task.prompt, source="queue", aspect_ratio=task.aspect_ratio)

        try:
            batch_start = time.time()
            async for batch in self._gateway.stream_batches(
                prompt=task.prompt,
                aspect_ratio=task.aspect_ratio,
                enable_pro=task.enable_pro,
                session_dir=session_dir,
                stop_event=stop_event,
                interval_seconds=task.interval_seconds,
                max_batches=-1,
            ):
                batch_ts = time.time()
                duration_ms = int((batch_ts - batch_start) * 1000)
                batch_start = batch_ts
                task.generated_count += len(batch)
                logger.info(
                    "task queue batch: task=%s +%d total=%d/%d",
                    task.id[:8], len(batch), task.generated_count, task.target_count,
                )
                if self._log_db:
                    model = "grok-imagine-pro" if task.enable_pro else "grok-imagine"
                    self._log_db.log_image(
                        request_id=task.session_id,
                        model=model,
                        prompt=task.prompt,
                        image_paths=[img.serve_path for img in batch],
                        image_count=len(batch),
                        aspect_ratio=task.aspect_ratio,
                        source="queue",
                        status="success",
                        duration_ms=duration_ms,
                    )
                if task.generated_count >= task.target_count:
                    stop_event.set()
                    break
                # 被外部暂停/取消？
                if task.status not in ("running",):
                    stop_event.set()
                    break

        except GrokClientError as exc:
            async with self._lock:
                task.failed_count += 1
                task.error = str(exc)
                if task.status == "running":
                    task.status = "failed"
                    task.finished_at = time.time()
            logger.warning("task queue task failed: task=%s error=%s", task.id[:8], exc)
            return
        except Exception as exc:
            async with self._lock:
                task.failed_count += 1
                task.error = str(exc)
                if task.status == "running":
                    task.status = "failed"
                    task.finished_at = time.time()
            logger.warning("task queue task unexpected error: task=%s error=%s", task.id[:8], exc)
            return
        finally:
            self._current_stop = None
            # CancelledError 是 BaseException，不被 except Exception 捕获，在此兜底
            if task.status == "running":
                task.status = "failed"
                task.error = task.error or "Task interrupted"
                task.finished_at = time.time()
            return  # 已在 finally 处理，跳过下方正常完成逻辑

        async with self._lock:
            if task.status == "running":
                if task.generated_count >= task.target_count:
                    task.status = "done"
                else:
                    task.status = "pending"  # 未到目标，重新入队（可能被暂停后恢复）
                task.finished_at = time.time()

        # 立即唤醒 worker 处理下一个任务
        self._wake.set()
