"""BUG-E 回归测试：image_stream._run 在 log_image 时记录正确的 account_label。

历史问题：stream 路径里 log_db.log_image() 没传 account_label，导致日志表该字段为空。
修复方案：ws_gateway._run_job 把 acq.label 写入 job.last_used_label，
stream_batches generator 通过 stats_sink dict 把它回传给 caller，
image_stream._run 在每次 yield 后用此 label 调 log_image。
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import unittest
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(_REPO_ROOT)

from mini_grok_api.image_stream import ImageStreamWorker, StreamConfig  # noqa: E402
from mini_grok_api.grok_client import ImageResult  # noqa: E402


def _make_fake_image(order: int = 0) -> ImageResult:
    """构造最小 ImageResult；仅 serve_path / order 字段被 image_stream 读。"""
    img = MagicMock(spec=ImageResult)
    img.serve_path = f"fake-session/img-{order}.jpg"
    img.order = order
    return img


class StreamLogAccountLabelTests(unittest.IsolatedAsyncioTestCase):
    async def test_log_image_receives_account_label_from_stats_sink(self):
        """ws_gateway 写 stats_sink['account_label'] → log_image 调用 account_label=该值。"""
        worker = ImageStreamWorker()

        # 模拟 ws_gateway.stream_batches：yield 一批 + 在 yield 前把 label 写入 stats_sink
        async def fake_stream_batches(*, stats_sink: dict, **kwargs) -> AsyncGenerator[list, None]:
            stats_sink["account_label"] = "free-1"
            yield [_make_fake_image(0), _make_fake_image(1)]
            # 第二批换账号（模拟 LRU 切换）
            stats_sink["account_label"] = "default"
            yield [_make_fake_image(2)]

        fake_gateway = MagicMock()
        fake_gateway.stream_batches = fake_stream_batches

        fake_log_db = MagicMock()
        worker._log_db = fake_log_db

        cfg = StreamConfig(
            prompt="test prompt", model="grok-imagine-image-lite",
            n=2, size="1024x1024", interval_seconds=1, max_rounds=2,
            max_images=0, enable_pro=False, image_data=None,
        )
        # 避免真实落盘 session dir
        with patch("mini_grok_api.image_stream._init_session", return_value=pathlib.Path("/tmp/fake-stream-test")):
            await worker._run(fake_gateway, cfg, "test-session-id")

        # 断言：log_image 被调用两次，account_label 分别为 free-1 / default
        calls = fake_log_db.log_image.call_args_list
        self.assertEqual(len(calls), 2, "应有两次 log_image (两轮 yield)")
        self.assertEqual(calls[0].kwargs.get("account_label"), "free-1",
                         "第一轮 log_image 应记录 free-1")
        self.assertEqual(calls[1].kwargs.get("account_label"), "default",
                         "第二轮 log_image 应记录 default")
        # 关键字段也都对
        for c in calls:
            self.assertEqual(c.kwargs.get("source"), "stream")
            self.assertEqual(c.kwargs.get("status"), "success")

    async def test_log_image_uses_empty_string_when_stats_sink_unset(self):
        """ws_gateway 没写 account_label → log_image 传空字符串（不应 KeyError）。"""
        worker = ImageStreamWorker()

        async def fake_stream_batches(*, stats_sink: dict, **kwargs):
            # 故意不写 account_label
            yield [_make_fake_image(0)]

        fake_gateway = MagicMock()
        fake_gateway.stream_batches = fake_stream_batches
        fake_log_db = MagicMock()
        worker._log_db = fake_log_db

        cfg = StreamConfig(
            prompt="x", model="grok-imagine-image-lite", n=1, size="1024x1024",
            interval_seconds=1, max_rounds=1, max_images=0,
            enable_pro=False, image_data=None,
        )
        with patch("mini_grok_api.image_stream._init_session", return_value=pathlib.Path("/tmp/fake-stream-test-2")):
            await worker._run(fake_gateway, cfg, "test-session-2")

        calls = fake_log_db.log_image.call_args_list
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs.get("account_label"), "")


if __name__ == "__main__":
    unittest.main()
