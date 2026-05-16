"""Grok 图片 WS 唯一入口 — 所有生图请求串行通过同一个 worker。

设计：
- 单条 asyncio.Queue，单 worker task，同一时刻最多一条 WS 连接
- 两种 job 类型：
    oneshot   → 收完第一批返回 list[ImageResult]（/v1/images/generations）
    streaming → 持续收批，通过 asyncio.Queue 向调用方 yield（stream worker / task queue）
- WS 断线后自动重连并继续当前 job（不丢弃），重连计入同一 session
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable

import aiohttp
from aiohttp_socks import ProxyConnector

from .config import Settings
from .grok_client import (
    GrokClientError,
    ImageResult,
    _build_imagine_msg,
    _build_reset_msg,
    _collect_batch,
    _ws_connect,
    _ws_headers,
)

if TYPE_CHECKING:
    from .accounts import AccountPool

logger = logging.getLogger(__name__)

_SENTINEL = None  # 用于终止 worker 的哨兵值


@dataclass
class _WsJob:
    session_dir: Path
    prompt: str
    aspect_ratio: str
    enable_pro: bool
    stop_event: asyncio.Event
    interval_seconds: float
    max_batches: int              # -1 = 无限
    on_batch: Callable[[list[ImageResult]], None]
    on_error: Callable[[Exception], None]
    on_done: Callable[[], None]
    image_data: str | None = None
    moderated_count: int = 0      # 累计被审核数（部分审核也会增加）
    total_attempted: int = 0      # 累计 slot 总数
    # X-Account-Label 透传：worker 内 acquire() 时强制走该账号；None=默认 LRU
    force_label: str | None = None


class WsGateway:
    """图片 WS 串行网关 — 整个项目唯一 WS 入口。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[_WsJob | None] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._settings_getter: Any = None
        self._account_pool: "AccountPool | None" = None
        self._current_job: _WsJob | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self, settings_getter: Any, account_pool: "AccountPool | None" = None) -> None:
        self._settings_getter = settings_getter
        self._account_pool = account_pool
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._worker_loop(), name="ws-gateway"
            )
            logger.info("WsGateway worker started")

    def stop(self) -> None:
        self._queue.put_nowait(_SENTINEL)

    def queue_depth(self) -> int:
        return self._queue.qsize()

    def is_busy(self) -> bool:
        return self._current_job is not None

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    async def generate_images(
        self,
        prompt: str,
        aspect_ratio: str,
        enable_pro: bool,
        session_dir: Path,
        image_data: str | None = None,
        force_label: str | None = None,
    ) -> list[ImageResult]:
        """一次性生图：提交 job，等待第一批完成后返回。

        force_label: X-Account-Label 透传；非 None 时强制走该账号（pool.acquire
        的 strict 模式：账号不存在/禁用 → raise UnknownAccountError /
        AccountDisabledError，上层应在调用前预校验避免 worker raise 500）。
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[list[ImageResult]] = loop.create_future()
        stop = asyncio.Event()

        def on_batch(batch: list[ImageResult]) -> None:
            if not fut.done():
                fut.set_result(batch)
            stop.set()  # 取到一批即停

        def on_error(exc: Exception) -> None:
            if not fut.done():
                fut.set_exception(exc)

        def on_done() -> None:
            if not fut.done():
                fut.set_exception(
                    GrokClientError("Imagine returned no images", status_code=502, code="no_images")
                )

        job = _WsJob(
            session_dir=session_dir,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            enable_pro=enable_pro,
            stop_event=stop,
            interval_seconds=0,
            max_batches=1,
            on_batch=on_batch,
            on_error=on_error,
            on_done=on_done,
            image_data=image_data,
            force_label=force_label,
        )
        await self._queue.put(job)
        return await fut

    async def stream_batches(
        self,
        prompt: str,
        aspect_ratio: str,
        enable_pro: bool,
        session_dir: Path,
        stop_event: asyncio.Event,
        interval_seconds: float,
        max_batches: int,
        image_data: str | None = None,
        stats_sink: dict | None = None,  # 可选：用于回传 moderated/total_attempted 累计
        force_label: str | None = None,   # X-Account-Label 透传（同 generate_images）
    ) -> AsyncGenerator[list[ImageResult], None]:
        """持续生图：提交 job，通过 async generator yield 每批结果。

        stats_sink 字典会在每次 yield 前被更新为最新的 {moderated, attempted} 计数；
        外层任务可读取以累计被审核数量。"""
        batch_q: asyncio.Queue[list[ImageResult] | BaseException | None] = asyncio.Queue()

        def on_batch(batch: list[ImageResult]) -> None:
            batch_q.put_nowait(batch)

        def on_error(exc: Exception) -> None:
            batch_q.put_nowait(exc)

        def on_done() -> None:
            batch_q.put_nowait(_SENTINEL)

        job = _WsJob(
            session_dir=session_dir,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            enable_pro=enable_pro,
            stop_event=stop_event,
            interval_seconds=interval_seconds,
            max_batches=max_batches,
            on_batch=on_batch,
            on_error=on_error,
            on_done=on_done,
            image_data=image_data,
            force_label=force_label,
        )
        await self._queue.put(job)

        while True:
            item = await batch_q.get()
            if stats_sink is not None:
                stats_sink["moderated"] = job.moderated_count
                stats_sink["attempted"] = job.total_attempted
            if item is _SENTINEL:
                return
            if isinstance(item, BaseException):
                raise item
            yield item  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    async def _worker_loop(self) -> None:
        while True:
            job = await self._queue.get()
            if job is _SENTINEL:
                logger.info("WsGateway worker stopping")
                break
            self._current_job = job
            try:
                await self._run_job(job)
            except Exception as exc:
                logger.warning("WsGateway job failed: %s", exc)
                try:
                    job.on_error(exc)
                except Exception:
                    pass
                try:
                    job.on_done()
                except Exception:
                    pass
            finally:
                self._current_job = None

    async def _run_job(self, job: _WsJob) -> None:
        """执行单个 job，含断线重连逻辑。"""
        # 优先使用 account_pool（Phase 1 多账号），降级到 settings_getter（兼容无池场景）
        # job.force_label 来自 endpoint X-Account-Label 透传；endpoint 已预校验，
        # 但 race condition 仍可能 raise（admin 同时删账号）— 由外层 try/except 兜底。
        if self._account_pool is not None:
            _acq_ctx = self._account_pool.acquire(
                model_id="grok-imagine", force_label=job.force_label
            )
            _acq = _acq_ctx.__enter__()
            settings: Settings = _acq.settings
        else:
            _acq_ctx = None
            _acq = None
            settings = self._settings_getter()
        _job_error: BaseException | None = None
        try:
            await self._run_job_inner(job, settings)
        except GrokClientError as exc:
            _job_error = exc
            if _acq is not None:
                _acq.mark_failure(exc.code or "upstream_5xx", retry_after=exc.retry_after)
            raise
        except Exception as exc:
            _job_error = exc
            if _acq is not None:
                _acq.mark_failure("upstream_5xx")
            raise
        finally:
            if _acq_ctx is not None:
                _acq_ctx.__exit__(
                    type(_job_error) if _job_error else None,
                    _job_error,
                    _job_error.__traceback__ if _job_error else None,
                )

    async def _run_job_inner(self, job: _WsJob, settings: Settings) -> None:
        """_run_job 的实际实现（已解耦 account_pool）。"""
        timeout = settings.grok_timeout_seconds
        proxy = settings.grok_proxy or None
        batch_count = 0
        consecutive_errors = 0
        consecutive_empty_closes = 0  # 连续「建连即断、0 帧」次数，用于检测 rate limit
        _MAX_CONSECUTIVE_ERRORS = 5
        _MAX_EMPTY_CLOSES = 3  # 连续 3 次空断 → 判定为图片额度受限

        while not job.stop_event.is_set():
            if job.max_batches >= 0 and batch_count >= job.max_batches:
                break

            try:
                # 代理类型分流：SOCKS5 用 ProxyConnector，HTTP/无代理用 TCPConnector
                # ssl: 默认 True（系统 cert），仅当 settings.grok_disable_ssl_verify=True 时跳过校验
                _ssl_arg: bool = not settings.grok_disable_ssl_verify
                if proxy and (proxy.startswith("socks5://") or proxy.startswith("socks://") or proxy.startswith("socks5h://")):
                    connector = ProxyConnector.from_url(proxy, ssl=_ssl_arg)
                    ws_proxy = None  # SOCKS5 已在 connector 层接管，不再传给 ws_connect
                else:
                    connector = aiohttp.TCPConnector(ssl=_ssl_arg)
                    ws_proxy = proxy
                async with aiohttp.ClientSession(
                    connector=connector,
                    headers=_ws_headers(settings),
                ) as http_sess:
                    ws = await _ws_connect(http_sess, timeout, ws_proxy,
                                            disable_ssl_verify=settings.grok_disable_ssl_verify)
                    async with ws:
                        await ws.send_json(_build_reset_msg())
                        await ws.send_json(_build_imagine_msg(
                            str(uuid.uuid4()),
                            job.prompt, job.aspect_ratio, job.enable_pro,
                            is_initial=True,
                            image_data=job.image_data,
                        ))
                        logger.info(
                            "WsGateway WS open: prompt=%r batches_so_far=%d",
                            job.prompt[:40], batch_count,
                        )

                        while not job.stop_event.is_set():
                            if job.max_batches >= 0 and batch_count >= job.max_batches:
                                return

                            deadline = asyncio.get_event_loop().time() + timeout
                            outcome = await _collect_batch(
                                ws, deadline, session_dir=job.session_dir
                            )
                            results = outcome.results
                            ws_closed = outcome.ws_closed

                            # 累计被审计数和总尝试数（无论本批是否有产出）
                            job.total_attempted += outcome.total_slots
                            job.moderated_count += outcome.moderated_count
                            if results:
                                job.on_batch(results)
                                batch_count += 1
                            if outcome.moderated_count > 0:
                                logger.info(
                                    "batch moderated stats: %d/%d images moderated (cumulative: %d/%d)",
                                    outcome.moderated_count, outcome.total_slots,
                                    job.moderated_count, job.total_attempted,
                                )

                            # 整批全审 + 0 产出 → 抛错让上层标失败
                            if (outcome.total_slots > 0
                                    and outcome.moderated_count == outcome.total_slots
                                    and not results):
                                raise GrokClientError(
                                    f"本批 {outcome.total_slots} 张全部被 Grok 内容审核拦截 (moderated)",
                                    status_code=403, code="all_moderated",
                                )

                            if ws_closed:
                                if not results and outcome.total_slots == 0:
                                    consecutive_empty_closes += 1
                                    logger.warning(
                                        "WsGateway WS closed with 0 frames (%d/%d), may be rate limited",
                                        consecutive_empty_closes, _MAX_EMPTY_CLOSES,
                                    )
                                    if consecutive_empty_closes >= _MAX_EMPTY_CLOSES:
                                        raise GrokClientError(
                                            "Imagine upstream error: Image rate limit exceeded"
                                            f" (WS closed {consecutive_empty_closes}× with no frames)",
                                            status_code=429, code="image_rate_limited",
                                        )
                                else:
                                    consecutive_empty_closes = 0
                                logger.info("WsGateway WS closed by server, reconnecting")
                                break  # 外层 while 重连

                            if job.stop_event.is_set():
                                return
                            if job.max_batches >= 0 and batch_count >= job.max_batches:
                                return

                            # 间隔 + jitter
                            jitter = random.uniform(1.5, 4.0)
                            wait = job.interval_seconds + jitter
                            logger.info(
                                "WsGateway interval: %.1fs (base=%.1f +jitter=%.1f) batch=%d",
                                wait, job.interval_seconds, jitter, batch_count,
                            )
                            try:
                                await asyncio.wait_for(job.stop_event.wait(), timeout=wait)
                                return  # stop_event 触发
                            except asyncio.TimeoutError:
                                pass

                            # 同一 WS 连接上发 input_text（is_initial=False）触发下一批
                            try:
                                await ws.send_json(_build_imagine_msg(
                                    str(uuid.uuid4()),
                                    job.prompt, job.aspect_ratio, job.enable_pro,
                                    is_initial=False,
                                ))
                                logger.info(
                                    "WsGateway next batch request: batch=%d",
                                    batch_count + 1,
                                )
                            except Exception as exc:
                                logger.warning("WsGateway next batch send failed: %s", exc)
                                break  # 重连

            except GrokClientError:
                raise
            except Exception as exc:
                if job.stop_event.is_set():
                    return
                consecutive_errors += 1
                if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    raise GrokClientError(
                        f"WsGateway failed after {consecutive_errors} consecutive errors: {exc}",
                        status_code=502,
                    ) from exc
                logger.warning(
                    "WsGateway session error (%d/%d): %s, retrying in 3s",
                    consecutive_errors, _MAX_CONSECUTIVE_ERRORS, exc,
                )
                await asyncio.sleep(3.0)
            else:
                consecutive_errors = 0  # 本次连接成功，重置计数

        job.on_done()
