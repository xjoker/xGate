"""集成测试：X-Account-Label header 客户端覆盖账号选择。

覆盖：
- strict 模式：label 不存在 → 400 + account_label_not_found
- strict 模式：label 被 manually_disabled → 400 + account_label_disabled
- valid label：acquire(force_label=...) 实际选中该账号
- 缺失/空白 header：保持默认 LRU 行为（不影响现有测试）
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
from mini_grok_api.accounts import (  # noqa: E402
    AccountDisabledError,
    UnknownAccountError,
    account_pool,
)

TEST_API_KEY = "x-account-label-test"


def _override_settings():
    base = main_mod.settings_store.get()
    new = replace(base, api_key=TEST_API_KEY)
    main_mod.settings_store._settings = new  # type: ignore[attr-defined]


def _headers(label: str | None = None) -> dict:
    h: dict = {"Authorization": f"Bearer {TEST_API_KEY}"}
    if label is not None:
        h["X-Account-Label"] = label
    return h


def _setup_pool() -> None:
    for info in account_pool.list_accounts():
        account_pool.delete_account(info.label)


class AcquireStrictModeTests(unittest.TestCase):
    """直接测 account_pool.acquire(force_label=...) 的 strict 语义。"""

    def setUp(self) -> None:
        _setup_pool()

    def test_unknown_label_raises(self):
        with self.assertRaises(UnknownAccountError) as ctx:
            account_pool.acquire(force_label="ghost").__enter__()
        self.assertEqual(ctx.exception.label, "ghost")

    def test_manually_disabled_label_raises(self):
        from mini_grok_api.accounts import Account
        account_pool.upsert_account(Account(
            label="dis-acc", cookie="sso=x", user_agent="", browser="chrome142",
            proxy="", statsig_id="", enabled=False, priority=1, weight=10,
        ))
        with self.assertRaises(AccountDisabledError) as ctx:
            account_pool.acquire(force_label="dis-acc").__enter__()
        self.assertEqual(ctx.exception.label, "dis-acc")
        self.assertEqual(ctx.exception.status, "manually_disabled")

    def test_valid_label_returns_that_account(self):
        from mini_grok_api.accounts import Account
        account_pool.upsert_account(Account(
            label="acc-a", cookie="sso=a", user_agent="", browser="chrome142",
            proxy="", statsig_id="", enabled=True, priority=5, weight=10,
        ))
        account_pool.upsert_account(Account(
            label="acc-b", cookie="sso=b", user_agent="", browser="chrome142",
            proxy="", statsig_id="", enabled=True, priority=1, weight=10,
        ))
        # acc-b 优先级更高（数字小），LRU 默认会选它；force_label=acc-a 应当强制走 acc-a
        with account_pool.acquire(force_label="acc-a") as acq:
            self.assertEqual(acq.label, "acc-a")


class XAccountLabelHeaderTests(unittest.TestCase):
    """通过 HTTP 端点验证 header → 400 错误响应正确成型。"""

    @classmethod
    def setUpClass(cls) -> None:
        _override_settings()
        cls.client = TestClient(main_mod.app)

    def setUp(self) -> None:
        _setup_pool()
        main_mod.limiter.reset()

    def test_unknown_label_returns_400_with_code(self):
        r = self.client.post("/v1/quota/chat", headers=_headers("nonexistent"))
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertEqual(body["error"]["code"], "account_label_not_found")
        self.assertEqual(body["error"]["param"], "X-Account-Label")

    def test_disabled_label_returns_400_with_status(self):
        from mini_grok_api.accounts import Account
        account_pool.upsert_account(Account(
            label="disabled-acc", cookie="sso=x", user_agent="", browser="chrome142",
            proxy="", statsig_id="", enabled=False, priority=1, weight=10,
        ))
        r = self.client.post("/v1/quota/chat", headers=_headers("disabled-acc"))
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertEqual(body["error"]["code"], "account_label_disabled")
        self.assertEqual(body["error"]["account_status"], "manually_disabled")

    def test_missing_header_falls_back_to_default(self):
        """无 header → 走默认 LRU；不应该抛 strict 错误（settings_fallback 触发）。"""
        # 没有任何账号 + 没有 header → acquire 返回 settings_fallback，不是 raise
        r = self.client.post("/v1/quota/chat", headers=_headers(None))
        # 因为 settings.grok_cookie 可能为空，这里允许 400 missing_grok_cookie 或 200
        # 关键是不能是 account_label_* 错误
        if r.status_code == 400:
            body = r.json()
            self.assertNotIn("account_label", body["error"].get("code", ""))

    def test_empty_header_treated_as_missing(self):
        """X-Account-Label: '' → 视为缺失，不触发 strict。"""
        r = self.client.post("/v1/quota/chat", headers=_headers(""))
        if r.status_code == 400:
            body = r.json()
            self.assertNotIn("account_label", body["error"].get("code", ""))


class ImagesGenerationsXAccountLabelTests(unittest.TestCase):
    """POST /v1/images/generations 现在也支持 X-Account-Label header (0.3.2)。

    endpoint 在提交 ws_gateway job 前预校验 label，避免 worker 内 raise 转 500。
    实际生图不跑（依赖 Grok 上游），只验证 label 校验路径。
    """

    @classmethod
    def setUpClass(cls) -> None:
        _override_settings()
        cls.client = TestClient(main_mod.app)

    def setUp(self) -> None:
        _setup_pool()

    def test_images_generations_unknown_label_returns_400(self):
        r = self.client.post(
            "/v1/images/generations",
            headers=_headers("nonexistent"),
            json={"prompt": "test", "model": "grok-imagine-image"},
        )
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertEqual(body["error"]["code"], "account_label_not_found")

    def test_images_generations_disabled_label_returns_400(self):
        from mini_grok_api.accounts import Account
        account_pool.upsert_account(Account(
            label="img-disabled", cookie="sso=x", user_agent="", browser="chrome142",
            proxy="", statsig_id="", enabled=False, priority=1, weight=10,
        ))
        r = self.client.post(
            "/v1/images/generations",
            headers=_headers("img-disabled"),
            json={"prompt": "test", "model": "grok-imagine-image"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "account_label_disabled")


class ImageStreamStartXAccountLabelTests(unittest.TestCase):
    """0.3.3: /v1/images/stream/start 接受 X-Account-Label，预校验同 images/generations。"""

    @classmethod
    def setUpClass(cls) -> None:
        _override_settings()
        cls.client = TestClient(main_mod.app)

    def setUp(self) -> None:
        _setup_pool()

    def test_stream_start_unknown_label_returns_400(self):
        r = self.client.post(
            "/v1/images/stream/start",
            headers=_headers("ghost-stream"),
            json={"prompt": "test", "model": "grok-imagine-image"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "account_label_not_found")

    def test_stream_start_disabled_label_returns_400(self):
        from mini_grok_api.accounts import Account
        account_pool.upsert_account(Account(
            label="stream-disabled", cookie="sso=x", user_agent="", browser="chrome142",
            proxy="", statsig_id="", enabled=False, priority=1, weight=10,
        ))
        r = self.client.post(
            "/v1/images/stream/start",
            headers=_headers("stream-disabled"),
            json={"prompt": "test", "model": "grok-imagine-image"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "account_label_disabled")


class VideoStatusPostTests(unittest.TestCase):
    """0.3.2: GET /v1/videos/{id}/status 被删除，POST /v1/videos/status 取代。"""

    @classmethod
    def setUpClass(cls) -> None:
        _override_settings()
        cls.client = TestClient(main_mod.app)

    def setUp(self) -> None:
        _setup_pool()

    def test_old_get_endpoint_returns_405_or_404(self):
        """旧 GET endpoint 必须已不存在。"""
        r = self.client.get("/v1/videos/some-id/status", headers=_headers())
        # FastAPI 路由未注册 → 404；405 也接受（如果有其他通配）
        self.assertIn(r.status_code, (404, 405))

    def test_post_endpoint_validates_body(self):
        r = self.client.post("/v1/videos/status", headers=_headers(), json={"video_id": ""})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "invalid_video_id")

    def test_post_endpoint_missing_body_returns_422(self):
        r = self.client.post("/v1/videos/status", headers=_headers(), json={})
        # pydantic missing field → 422
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
