"""连续后台生图 worker — 通过 WsGateway 串行生图。"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .grok_client import GrokClientError, _init_session, resolve_aspect_ratio

if TYPE_CHECKING:
    from .db import LogDB
    from .ws_gateway import WsGateway
    from .task_queue import TaskQueue

logger = logging.getLogger(__name__)


@dataclass
class StreamConfig:
    prompt: str
    model: str = "grok-imagine"
    n: int = 1
    size: str = "1024x1024"
    interval_seconds: float = 5.0
    max_rounds: int = -1     # 批次上限（兜底，-1=不限）
    max_images: int = 0      # 图片数上限（精确，0=不限；优先于 max_rounds 判定停止）
    enable_pro: bool = False
    image_data: str | None = None


@dataclass
class StreamStatus:
    running: bool = False
    current_round: int = 0
    success_count: int = 0
    failure_count: int = 0
    last_error: str = ""
    last_success_time: float = 0.0
    started_at: float = 0.0
    session_id: str = ""
    config: StreamConfig | None = None


class ImageStreamWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._status = StreamStatus()
        self._stop_event = asyncio.Event()
        self._log_db: "LogDB | None" = None
        self._task_queue: "TaskQueue | None" = None
        self._mirror_task_id: str = ""

    def status(self) -> dict[str, Any]:
        s = self._status
        cfg = s.config
        return {
            "running": s.running,
            "current_round": s.current_round,
            "success_count": s.success_count,
            "failure_count": s.failure_count,
            "last_error": s.last_error,
            "last_success_time": s.last_success_time,
            "started_at": s.started_at,
            "session_id": s.session_id,
            "config": {
                "prompt": cfg.prompt,
                "model": cfg.model,
                "size": cfg.size,
                "interval_seconds": cfg.interval_seconds,
                "max_rounds": cfg.max_rounds,
            } if cfg else None,
        }

    def is_running(self) -> bool:
        return self._status.running

    def start(self, gateway: "WsGateway", cfg: StreamConfig, log_db: "LogDB | None" = None,
              task_queue: "TaskQueue | None" = None) -> str:
        if self._status.running:
            return self._status.session_id
        self._stop_event.clear()
        self._log_db = log_db
        self._task_queue = task_queue
        session_id = str(uuid.uuid4())
        self._status = StreamStatus(
            running=True,
            started_at=time.time(),
            session_id=session_id,
            config=cfg,
        )
        # 镜像到 task_queue 列表（kind=stream），刷新页面也能看到
        self._mirror_task_id = "stream-" + session_id
        self._mirror_status("running")
        self._task = asyncio.create_task(
            self._run(gateway, cfg, session_id),
            name="image-stream-worker",
        )
        return session_id

    def _mirror_status(self, status: str) -> None:
        if not self._task_queue or not self._mirror_task_id:
            return
        s = self._status
        cfg = s.config
        if not cfg:
            return
        try:
            from .grok_client import resolve_aspect_ratio
            ar = resolve_aspect_ratio(cfg.size)
            self._task_queue.upsert_stream_mirror(
                task_id=self._mirror_task_id,
                prompt=cfg.prompt,
                session_id=s.session_id,
                aspect_ratio=ar,
                enable_pro=cfg.enable_pro,
                interval_seconds=cfg.interval_seconds,
                status=status,  # type: ignore
                generated_count=s.success_count,
                failed_count=s.failure_count,
                target_count=(cfg.max_images if cfg.max_images > 0
                              else (cfg.max_rounds * cfg.n if cfg.max_rounds > 0 else 0)),
                error=s.last_error,
                started_at=s.started_at,
                finished_at=time.time() if status not in ("running", "pending") else None,
            )
        except Exception as exc:
            logger.warning("stream mirror update failed: %s", exc)

    def stop(self) -> None:
        self._stop_event.set()
        self._status.running = False

    async def _run(self, gateway: "WsGateway", cfg: StreamConfig, session_id: str) -> None:
        aspect_ratio = resolve_aspect_ratio(cfg.size)
        session_dir = _init_session(session_id, prompt=cfg.prompt, source="stream", aspect_ratio=aspect_ratio)
        try:
            batch_start = time.time()
            async for batch in gateway.stream_batches(
                prompt=cfg.prompt,
                aspect_ratio=aspect_ratio,
                enable_pro=cfg.enable_pro,
                session_dir=session_dir,
                stop_event=self._stop_event,
                interval_seconds=cfg.interval_seconds,
                max_batches=cfg.max_rounds,
                image_data=cfg.image_data,
            ):
                batch_ts = time.time()
                duration_ms = int((batch_ts - batch_start) * 1000)
                batch_start = batch_ts
                self._status.current_round += 1
                self._status.success_count += len(batch)
                self._status.last_success_time = batch_ts
                logger.info(
                    "image stream batch done: round=%d saved=%d total=%d",
                    self._status.current_round, len(batch), self._status.success_count,
                )
                self._mirror_status("running")
                if self._log_db:
                    self._log_db.log_image(
                        request_id=session_id,
                        model=cfg.model,
                        prompt=cfg.prompt,
                        image_paths=[img.serve_path for img in batch],
                        image_count=len(batch),
                        aspect_ratio=aspect_ratio,
                        source="stream",
                        status="success",
                        duration_ms=duration_ms,
                    )
                # 按图片数严格停止：达到 max_images 就让 ws_gateway 停下
                if cfg.max_images > 0 and self._status.success_count >= cfg.max_images:
                    self._stop_event.set()
                    break
        except GrokClientError as exc:
            self._status.failure_count += 1
            self._status.last_error = str(exc)
            if self._log_db:
                self._log_db.log_image(
                    request_id=session_id, model=cfg.model, prompt=cfg.prompt,
                    image_count=0, aspect_ratio=aspect_ratio, source="stream",
                    status="error", duration_ms=int((time.time() - batch_start) * 1000),
                    error=str(exc),
                )
            logger.warning("image stream fatal error: %s", exc)
        except Exception as exc:
            self._status.failure_count += 1
            self._status.last_error = str(exc)
            if self._log_db:
                self._log_db.log_image(
                    request_id=session_id, model=cfg.model, prompt=cfg.prompt,
                    image_count=0, aspect_ratio=aspect_ratio, source="stream",
                    status="error", duration_ms=int((time.time() - batch_start) * 1000),
                    error=str(exc),
                )
            logger.warning("image stream unexpected error: %s", exc)
        finally:
            self._status.running = False
            logger.info("image stream worker stopped: total_saved=%d", self._status.success_count)
            # 终态镜像
            if self._status.last_error:
                self._mirror_status("failed")
            elif self._stop_event.is_set():
                self._mirror_status("cancelled")
            else:
                self._mirror_status("done")
