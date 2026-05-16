"""SAST round 5 修复的回归测试（v0.3.7）。

- P1: ImageStreamStartRequest / VideoGenerationRequest 的 image_data 加 max_length
- P2: mcp_session.cleanup 被 _conversation_binding_cleanup_loop 周期调用
- P2: _safe_proxy_content_type 白名单 + 默认 octet-stream
"""

from __future__ import annotations

import os
import pathlib
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(_REPO_ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from mini_grok_api import main as main_mod  # noqa: E402

TEST_API_KEY = "sast-round5-test-key"


def _override_settings():
    from dataclasses import replace
    base = main_mod.settings_store.get()
    new = replace(base, api_key=TEST_API_KEY)
    main_mod.settings_store._settings = new  # type: ignore[attr-defined]


def _headers() -> dict:
    return {"Authorization": f"Bearer {TEST_API_KEY}"}


class ImageDataMaxLengthTests(unittest.TestCase):
    """P1: image_data 超过 20MB 应被 pydantic 拒绝（422）。"""

    @classmethod
    def setUpClass(cls) -> None:
        _override_settings()
        cls.client = TestClient(main_mod.app)

    def test_stream_start_huge_image_data_rejected(self):
        huge = "A" * 20_000_001  # 20MB + 1
        r = self.client.post(
            "/v1/images/stream/start",
            headers=_headers(),
            json={"prompt": "x", "model": "grok-imagine-image-lite", "image_data": huge},
        )
        self.assertEqual(r.status_code, 422)

    def test_videos_generate_huge_image_data_rejected(self):
        huge = "A" * 20_000_001
        r = self.client.post(
            "/v1/videos/generate",
            headers=_headers(),
            json={"prompt": "x", "image_data": huge},
        )
        self.assertEqual(r.status_code, 422)

    def test_stream_start_small_image_data_passes_schema(self):
        """正常大小 (1KB) 不被 schema 拒，进入业务逻辑（可能 4xx 但非 422）。"""
        small = "A" * 1024
        r = self.client.post(
            "/v1/images/stream/start",
            headers=_headers(),
            json={"prompt": "x", "model": "grok-imagine-image-lite", "image_data": small},
        )
        # 不应是 422（schema 校验过）
        self.assertNotEqual(r.status_code, 422)


class SafeProxyContentTypeTests(unittest.TestCase):
    """P2: 上游 Content-Type 白名单"""

    def test_image_types_preserved(self):
        from mini_grok_api.main import _safe_proxy_content_type
        self.assertEqual(_safe_proxy_content_type("image/jpeg"), "image/jpeg")
        self.assertEqual(_safe_proxy_content_type("image/png"), "image/png")
        self.assertEqual(_safe_proxy_content_type("video/mp4"), "video/mp4")
        self.assertEqual(_safe_proxy_content_type("audio/mpeg"), "audio/mpeg")

    def test_image_with_charset_preserved(self):
        from mini_grok_api.main import _safe_proxy_content_type
        self.assertEqual(_safe_proxy_content_type("image/png; charset=utf-8"),
                         "image/png; charset=utf-8")

    def test_dangerous_types_downgraded(self):
        from mini_grok_api.main import _safe_proxy_content_type
        for bad in ("text/html", "text/javascript", "application/xhtml+xml",
                    "application/x-msdownload", "text/plain"):
            self.assertEqual(_safe_proxy_content_type(bad), "application/octet-stream",
                             f"{bad} 应该被降级")

    def test_none_or_empty_returns_octet_stream(self):
        from mini_grok_api.main import _safe_proxy_content_type
        self.assertEqual(_safe_proxy_content_type(None), "application/octet-stream")
        self.assertEqual(_safe_proxy_content_type(""), "application/octet-stream")

    def test_octet_and_json_passthrough(self):
        from mini_grok_api.main import _safe_proxy_content_type
        self.assertEqual(_safe_proxy_content_type("application/octet-stream"),
                         "application/octet-stream")
        self.assertEqual(_safe_proxy_content_type("application/json"), "application/json")


class McpSessionCleanupTests(unittest.TestCase):
    """P2: cleanup loop 真的会调 mcp_session.cleanup()"""

    def test_mcp_session_cleanup_function_callable(self):
        """sanity: cleanup() 不会 raise"""
        from mini_grok_api import mcp_session
        # 先 upsert 一条
        mcp_session.upsert("test-mcp-session", "conv-xxx", "resp-yyy")
        self.assertEqual(mcp_session.get("test-mcp-session"), ("conv-xxx", "resp-yyy"))
        # 用 max_age_seconds=0 强制清掉
        deleted = mcp_session.cleanup(max_age_seconds=0)
        self.assertGreaterEqual(deleted, 1)
        self.assertEqual(mcp_session.get("test-mcp-session"), (None, None))

    def test_cleanup_loop_imports_mcp_session(self):
        """sanity: _conversation_binding_cleanup_loop 函数体里 import mcp_session 不报错"""
        import inspect
        from mini_grok_api.main import _conversation_binding_cleanup_loop
        src = inspect.getsource(_conversation_binding_cleanup_loop)
        self.assertIn("mcp_session", src)
        self.assertIn(".cleanup()", src)


if __name__ == "__main__":
    unittest.main()
