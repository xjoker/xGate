"""图片生成任务队列 — 支持优先级、暂停、恢复、取消、重排。"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from typing import TYPE_CHECKING

from .grok_client import GrokClientError, _init_session

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
    kind: str = "manual"   # manual=队列任务，stream=连续生成镜像（不被 worker 调度）
    origin: str = "queue"  # 创建来源：queue=任务面板，chat=Chat 模式 +队列按钮，api=外部 API
    generated_count: int = 0
    failed_count: int = 0
    moderated_count: int = 0  # 被审核拦截累计数
    attempt_cap: int = 0       # 尽力模式上限：>0 时启用，等于 total_attempts 允许达到的最大值
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "origin": self.origin,
            "prompt": self.prompt,
            "target_count": self.target_count,
            "generated_count": self.generated_count,
            "failed_count": self.failed_count,
            "moderated_count": self.moderated_count,
            "attempt_cap": self.attempt_cap,
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
        self._restore_tasks()
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._worker_loop(), name="task-queue-worker"
            )

    def stop_worker(self) -> None:
        if self._current_stop:
            self._current_stop.set()
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()

    def _restore_tasks(self) -> None:
        if not self._log_db:
            return
        rows = self._log_db.load_tasks()
        for r in rows:
            status = r["status"]
            if status == "running":
                # stream 类型由 ImageStreamWorker 驱动，重启后无法继续 → 标 cancelled
                # manual 类型由 TaskQueue 自己驱动 → 重置 pending 让 worker 接管
                kind = r.get("kind", "manual")
                status = "cancelled" if kind == "stream" else "pending"
            task = ImageTask(
                id=r["id"],
                kind=r.get("kind", "manual"),
                origin=r.get("origin", "queue"),
                prompt=r["prompt"],
                target_count=r["target_count"],
                aspect_ratio=r["aspect_ratio"],
                enable_pro=r["enable_pro"],
                interval_seconds=r["interval_seconds"],
                status=status,
                priority=r["priority"],
                session_id=r["session_id"],
                generated_count=r["generated_count"],
                failed_count=r["failed_count"],
                moderated_count=r.get("moderated_count", 0),
                attempt_cap=r.get("attempt_cap", 0),
                created_at=r["created_at"],
                started_at=r.get("started_at"),
                finished_at=r.get("finished_at"),
                error=r.get("error", ""),
            )
            self._tasks[task.id] = task
            if r["status"] != status:  # running → pending 需要写回 DB
                self._save_task(task)
            if status == "pending":
                self._wake.set()
        logger.info("task queue: restored %d tasks from db", len(rows))

    def _save_task(self, task: "ImageTask") -> None:
        if self._log_db:
            self._log_db.save_task(task.to_dict())

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
        origin: str = "queue",
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
                origin=origin,
            )
            self._tasks[task.id] = task
            self._wake.set()
            self._save_task(task)
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
                self._save_task(task)
                return True
            if task.status == "pending":
                task.status = "paused"
                self._save_task(task)
                return True
            return False

    async def resume_task(self, task_id: str) -> bool:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "paused":
                return False
            task.status = "pending"
            self._wake.set()
            self._save_task(task)
            return True

    async def close_task(self, task_id: str) -> bool:
        """把 failed 任务关闭为 done — 错误/审核信息保留只读。
        用户场景：接受当前部分结果，不再让系统折腾。"""
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            if task.status != "failed":
                return False
            task.status = "done"
            task.finished_at = task.finished_at or time.time()
            # error / failed_count / moderated_count 保留不动，作为历史说明
            self._save_task(task)
            return True

    async def retry_task(self, task_id: str) -> bool:
        """重试 failed/cancelled 任务 — 原地复用同一条任务记录。

        - manual：直接重置计数 + 转 pending，worker 接管
        - stream：原镜像 kind 改为 manual，让 TaskQueue worker 接管同一任务
          （原 ImageStreamWorker 已退出，但任务定义可在 task_queue 内继续跑）
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            if task.status not in ("failed", "cancelled"):
                return False
            # stream → manual 转换：保留同一 task_id 和历史，只是换驱动方式
            if task.kind == "stream":
                task.kind = "manual"
                # stream 任务可能 target_count 是按 max_rounds 估算的，确保至少 4 张
                if task.target_count <= 0:
                    task.target_count = 4
            # 重置状态
            task.status = "pending"
            task.generated_count = 0
            task.failed_count = 0
            task.moderated_count = 0
            task.attempt_cap = 0
            task.error = ""
            task.started_at = None
            task.finished_at = None
            self._wake.set()
            self._save_task(task)
            return True

    async def enable_best_effort(self, task_id: str) -> bool:
        """对 failed 任务或"done 但成功数未达 target"的任务启用尽力模式：保留已有进度，
        允许再尝试 target_count*5 次。
        - 接受 status=failed
        - 接受 status=done 且 generated_count < target_count（被审核拦截太多导致提前判 done 的情况）
        - 不重置 generated/moderated/failed 计数
        - 设置 attempt_cap = 当前总尝试数 + target_count * 5
        - 转 pending 让 worker 接管；耗尽 cap 时视为成功（done）
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            eligible = (
                task.status == "failed"
                or (task.status == "done" and task.generated_count < task.target_count)
            )
            if not eligible:
                return False
            current_attempts = task.generated_count + task.moderated_count + task.failed_count
            task.attempt_cap = current_attempts + max(task.target_count, 1) * 5
            task.status = "pending"
            task.error = ""
            task.finished_at = None
            self._wake.set()
            self._save_task(task)
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
            self._save_task(task)
            return True

    async def remove_task(self, task_id: str) -> bool:
        await self.cancel_task(task_id)
        async with self._lock:
            if task_id in self._tasks:
                del self._tasks[task_id]
                if self._log_db:
                    self._log_db.delete_task(task_id)
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
                self._save_task(task)
                self._save_task(neighbor)
                return True
            if direction == "down" and idx < len(sorted_tasks) - 1:
                neighbor = sorted_tasks[idx + 1]
                task.priority, neighbor.priority = neighbor.priority, task.priority
                self._save_task(task)
                self._save_task(neighbor)
                return True
            return False

    def upsert_stream_mirror(
        self, *, task_id: str, prompt: str, session_id: str,
        aspect_ratio: str, enable_pro: bool, interval_seconds: float,
        status: TaskStatus, generated_count: int, failed_count: int = 0,
        target_count: int = 0, error: str = "",
        started_at: float | None = None, finished_at: float | None = None,
    ) -> None:
        """连续生成镜像到 task_queue（kind=stream），仅供查看，不被 worker 调度。"""
        task = self._tasks.get(task_id)
        if task is None:
            task = ImageTask(
                id=task_id,
                kind="stream",
                prompt=prompt,
                target_count=target_count,
                aspect_ratio=aspect_ratio,
                enable_pro=enable_pro,
                interval_seconds=interval_seconds,
                status=status,
                priority=-1,  # 流式排在最前
                session_id=session_id,
                generated_count=generated_count,
                failed_count=failed_count,
                started_at=started_at or time.time(),
                finished_at=finished_at,
                error=error,
            )
            self._tasks[task_id] = task
        else:
            task.status = status
            task.generated_count = generated_count
            task.failed_count = failed_count
            task.target_count = target_count or task.target_count
            task.error = error
            if started_at is not None: task.started_at = started_at
            if finished_at is not None: task.finished_at = finished_at
        self._save_task(task)

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
                # stream 任务只是镜像展示，由 ImageStreamWorker 自己驱动
                if task.kind == "stream":
                    continue
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
            self._save_task(task)

        logger.info(
            "task queue: running task=%s prompt=%r target=%d generated=%d",
            task.id[:8], task.prompt[:40], task.target_count, task.generated_count,
        )

        session_dir = _init_session(task.session_id, prompt=task.prompt, source="queue", aspect_ratio=task.aspect_ratio)

        completed_normally = False
        ws_stats: dict = {"moderated": 0, "attempted": 0}
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
                stats_sink=ws_stats,
            ):
                # 同步累计的被审核数
                task.moderated_count = ws_stats.get("moderated", 0)
                batch_ts = time.time()
                duration_ms = int((batch_ts - batch_start) * 1000)
                batch_start = batch_ts
                task.generated_count += len(batch)
                logger.info(
                    "task queue batch: task=%s +%d total=%d/%d",
                    task.id[:8], len(batch), task.generated_count, task.target_count,
                )
                if self._log_db:
                    model = "grok-imagine-image" if task.enable_pro else "grok-imagine-image-lite"
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
                # 完成判定
                attempted = task.generated_count + task.moderated_count
                if task.attempt_cap > 0:
                    # 尽力模式：达标(成功数到目标) 或 总尝试数到 cap 即停
                    total_attempts = task.generated_count + task.moderated_count + task.failed_count
                    if task.generated_count >= task.target_count or total_attempts >= task.attempt_cap:
                        stop_event.set()
                        break
                else:
                    # 普通模式：成功 + 被审 >= 目标尝试数 → 任务完成（即使 0 张成功）
                    if attempted >= task.target_count:
                        stop_event.set()
                        break
                # 被外部暂停/取消？
                if task.status not in ("running",):
                    stop_event.set()
                    break
                self._save_task(task)
            completed_normally = True

        except GrokClientError as exc:
            # all_moderated 是预期的部分失败，统计后判断是否够数；其他错误直接 failed
            is_moderation = (exc.code == "all_moderated" or exc.code == "silent_block")
            async with self._lock:
                if not is_moderation:
                    task.failed_count += 1
                task.moderated_count = ws_stats.get("moderated", task.moderated_count)
                task.error = str(exc)
                attempted = task.generated_count + task.moderated_count
                total_attempts = task.generated_count + task.moderated_count + task.failed_count
                if task.status == "running":
                    if task.attempt_cap > 0 and is_moderation:
                        # 尽力模式：审核拦截不算失败
                        if task.generated_count >= task.target_count or total_attempts >= task.attempt_cap:
                            task.status = "done"
                            task.finished_at = time.time()
                        else:
                            # 预算未耗尽 → 重新入队继续尝试
                            task.status = "pending"
                            self._wake.set()
                    elif is_moderation and attempted >= task.target_count:
                        # 配额内全审：仍标 done，error 字段保留作说明
                        task.status = "done"
                        task.finished_at = time.time()
                    else:
                        task.status = "failed"
                        task.finished_at = time.time()
            self._save_task(task)
            if self._log_db:
                model = "grok-imagine-image" if task.enable_pro else "grok-imagine-image-lite"
                self._log_db.log_image(
                    request_id=task.session_id, model=model, prompt=task.prompt,
                    image_count=0, aspect_ratio=task.aspect_ratio, source="queue",
                    status="error", duration_ms=int((time.time() - batch_start) * 1000),
                    error=str(exc),
                )
            logger.warning("task queue task ended with error: task=%s code=%s status=%s",
                           task.id[:8], exc.code, task.status)
            return
        except Exception as exc:
            async with self._lock:
                task.failed_count += 1
                task.error = str(exc)
                if task.status == "running":
                    task.status = "failed"
                    task.finished_at = time.time()
            self._save_task(task)
            if self._log_db:
                model = "grok-imagine-image" if task.enable_pro else "grok-imagine-image-lite"
                self._log_db.log_image(
                    request_id=task.session_id, model=model, prompt=task.prompt,
                    image_count=0, aspect_ratio=task.aspect_ratio, source="queue",
                    status="error", duration_ms=int((time.time() - batch_start) * 1000),
                    error=str(exc),
                )
            logger.warning("task queue task unexpected error: task=%s error=%s", task.id[:8], exc)
            return
        finally:
            self._current_stop = None
            if not completed_normally and task.status == "running":
                task.status = "failed"
                task.error = task.error or "Task interrupted"
                task.finished_at = time.time()
                self._save_task(task)

        async with self._lock:
            if task.status == "running":
                attempted = task.generated_count + task.moderated_count
                total_attempts = task.generated_count + task.moderated_count + task.failed_count
                if task.attempt_cap > 0:
                    # 尽力模式：达标或耗尽预算都算 done
                    if task.generated_count >= task.target_count or total_attempts >= task.attempt_cap:
                        task.status = "done"
                        if not task.error:
                            if task.generated_count >= task.target_count:
                                task.error = f"尽力模式完成：成功 {task.generated_count}/{task.target_count} 张"
                            else:
                                task.error = (
                                    f"尽力模式结束：用尽 {task.attempt_cap} 次尝试预算，"
                                    f"成功 {task.generated_count} 张，{task.moderated_count} 张被审"
                                )
                        task.finished_at = time.time()
                    else:
                        task.status = "pending"
                else:
                    # 普通模式：尝试次数（成功 + 被审）达到 target 就视为 done
                    if attempted >= task.target_count:
                        task.status = "done"
                        if task.moderated_count > 0 and not task.error:
                            task.error = f"完成：成功 {task.generated_count} 张，{task.moderated_count} 张被内容审核拦截"
                        task.finished_at = time.time()
                    else:
                        task.status = "pending"  # 未达目标重新入队（被暂停恢复场景）
                self._save_task(task)

        # 立即唤醒 worker 处理下一个任务
        self._wake.set()
