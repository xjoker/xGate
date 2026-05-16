"""集成测试：/v1/auth/login 的 slowapi 限流（10/分钟/IP）。

注意：conftest.py 的 autouse fixture `_reset_rate_limiter` 会在每个测试前
清空 limiter 状态，这里需要手动控制 — setUp 里再 reset 一次以保险。
"""

from __future__ import annotations

import os
import pathlib
import unittest
from dataclasses import replace

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(_REPO_ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from mini_grok_api import main as main_mod  # noqa: E402

TEST_API_KEY = "login-rate-limit-test"


def _override_settings(api_key: str = TEST_API_KEY):
    base = main_mod.settings_store.get()
    new = replace(base, api_key=api_key)
    main_mod.settings_store._settings = new  # type: ignore[attr-defined]
    return new


class LoginRateLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _override_settings(TEST_API_KEY)
        cls.client = TestClient(main_mod.app)

    def setUp(self) -> None:
        # 显式 reset：自定义 setUp 会在 autouse fixture 之后执行，但保险起见
        main_mod.limiter.reset()

    def test_first_10_requests_pass_auth_check(self):
        """10 次错误 api_key 都得到 401（鉴权失败），不是 429。"""
        for _ in range(10):
            r = self.client.post("/v1/auth/login", data={"api_key": "WRONG"})
            self.assertEqual(r.status_code, 401, "前 10 次应当走到鉴权层")

    def test_11th_request_returns_429_with_retry_after(self):
        """第 11 次 → 429 + Retry-After header + JSON error 体。"""
        for _ in range(10):
            self.client.post("/v1/auth/login", data={"api_key": "WRONG"})
        r = self.client.post("/v1/auth/login", data={"api_key": "WRONG"})
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)
        # Retry-After 必须是正整数秒数
        retry_after = int(r.headers["Retry-After"])
        self.assertGreater(retry_after, 0)
        body = r.json()
        self.assertEqual(body["error"]["type"], "rate_limit_error")
        self.assertEqual(body["error"]["code"], "login_rate_limited")
        self.assertEqual(body["error"]["retry_after"], retry_after)

    def test_valid_login_counts_against_limit(self):
        """成功登录也算配额（防止暴破工具用真凭证 + 错凭证混合绕过）。"""
        # 9 次成功 + 1 次失败 = 10 次配额耗尽
        for _ in range(9):
            r = self.client.post("/v1/auth/login", data={"api_key": TEST_API_KEY})
            self.assertEqual(r.status_code, 200)
        r = self.client.post("/v1/auth/login", data={"api_key": "WRONG"})
        self.assertEqual(r.status_code, 401)
        # 第 11 次必须 429
        r = self.client.post("/v1/auth/login", data={"api_key": TEST_API_KEY})
        self.assertEqual(r.status_code, 429)

    def test_rate_limit_headers_present_on_success(self):
        """headers_enabled=True → 正常 200 响应也带 X-RateLimit-* 头。"""
        r = self.client.post("/v1/auth/login", data={"api_key": TEST_API_KEY})
        self.assertEqual(r.status_code, 200)
        # slowapi 装 headers_enabled 后，正常响应也包含速率窗口指示
        self.assertIn("x-ratelimit-limit", {k.lower() for k in r.headers.keys()})
        self.assertIn("x-ratelimit-remaining", {k.lower() for k in r.headers.keys()})


if __name__ == "__main__":
    unittest.main()
