"""CSRF 安全回归测试：GET-有副作用端点 POST 化验证。

涵盖：
- /v1/quota/image     改为 POST：cookie+无CSRF → 403，Bearer → 200/正常错误
- /v1/quota/chat      改为 POST：cookie+无CSRF → 403，Bearer → 200/正常错误
- /v1/quota           改为 POST：cookie+无CSRF → 403，Bearer → 200/正常错误
- /admin/models/verify 改为 POST：cookie+无CSRF → 403，Bearer → 200/正常错误
- /admin/dashboard    改为 POST：cookie+无CSRF → 403，Bearer → 200/正常错误
- /v1/grok/assets     改为 POST（Wave 5）：cookie+无CSRF → 403，Bearer → 200/正常错误
"""

from __future__ import annotations

import os
import pathlib
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

# 项目根 cwd
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(_REPO_ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from mini_grok_api import main as main_mod  # noqa: E402
from mini_grok_api.auth_session import session_store  # noqa: E402
from mini_grok_api.config import Settings  # noqa: E402

TEST_API_KEY = "test-key-side-effects"


def _override_settings(api_key: str = TEST_API_KEY) -> Settings:
    base = main_mod.settings_store.get()
    new = replace(base, api_key=api_key, grok_cookie="fake-cookie-for-test")
    main_mod.settings_store._settings = new  # type: ignore[attr-defined]
    return new


# ---------------------------------------------------------------------------
# 共用 mock：避免真正去打 Grok 上游
# ---------------------------------------------------------------------------

_FAKE_QUOTA_RESPONSE = {
    "remainingQueries": 10,
    "totalQueries": 50,
    "windowSizeSeconds": 7200,
}

_FAKE_IMAGE_RATE_LIMITS = {
    "candidates": [
        {"model_name": "aurora", "remaining": 5, "total": 20, "used_pct": 75.0},
    ]
}

_FAKE_DASHBOARD = {
    "version": "0.0.0-test",
    "model_quotas": [],
    "image_quota": None,
    "tasks": {"total": 0, "pending": 0, "running": 0, "failed": 0, "paused": 0},
    "recent_tasks": [],
    "logs": {},
    "monitor": {
        "total_requests": 0,
        "success_count": 0,
        "failure_count": 0,
        "cloudflare_challenge": 0,
        "recent_error": None,
    },
    "cookie_configured": True,
    "proxy_configured": False,
}


class SideEffectEndpointCsrfTests(unittest.TestCase):
    """改为 POST 的端点：cookie 通道无 CSRF token 时应返回 403。"""

    @classmethod
    def setUpClass(cls) -> None:
        _override_settings(TEST_API_KEY)
        cls.client = TestClient(main_mod.app)

    def setUp(self) -> None:
        session_store.revoke_all()
        self.client.cookies.clear()
        _override_settings(TEST_API_KEY)

    def _login(self):
        """登录并返回 csrf_token。"""
        r = self.client.post("/v1/auth/login", data={"api_key": TEST_API_KEY})
        self.assertEqual(r.status_code, 200)
        return r.json()["csrf_token"]

    # ── /v1/quota/image ──────────────────────────────────────────────────────

    def test_quota_image_cookie_without_csrf_returns_403(self):
        """cookie 通道 POST 无 X-CSRF-Token → 403（CSRF 校验失败）。"""
        self._login()
        # 不带 X-CSRF-Token header，模拟 CSRF 攻击场景
        r = self.client.post("/v1/quota/image")
        self.assertEqual(r.status_code, 403)
        body = r.json()
        self.assertEqual(body["detail"]["code"], "csrf_failed")

    def test_quota_image_bearer_returns_non_401(self):
        """Bearer header 通道不受 CSRF 约束，能进入业务逻辑。"""
        with patch.object(
            main_mod, "query_image_rate_limits",
            new=AsyncMock(return_value=_FAKE_IMAGE_RATE_LIMITS),
        ):
            r = self.client.post(
                "/v1/quota/image",
                headers={"Authorization": f"Bearer {TEST_API_KEY}"},
            )
        self.assertNotEqual(r.status_code, 401)
        self.assertNotEqual(r.status_code, 403)

    def test_quota_image_old_get_returns_405(self):
        """旧 GET 路由已不存在，应返回 405 Method Not Allowed。"""
        r = self.client.get(
            "/v1/quota/image",
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
        )
        self.assertEqual(r.status_code, 405)

    # ── /v1/quota/chat ───────────────────────────────────────────────────────

    def test_quota_chat_cookie_without_csrf_returns_403(self):
        self._login()
        r = self.client.post("/v1/quota/chat")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"]["code"], "csrf_failed")

    def test_quota_chat_bearer_returns_non_401(self):
        with patch.object(
            main_mod, "query_rate_limits",
            new=AsyncMock(return_value=_FAKE_QUOTA_RESPONSE),
        ):
            r = self.client.post(
                "/v1/quota/chat",
                headers={"Authorization": f"Bearer {TEST_API_KEY}"},
            )
        self.assertNotEqual(r.status_code, 401)
        self.assertNotEqual(r.status_code, 403)

    def test_quota_chat_old_get_returns_405(self):
        r = self.client.get(
            "/v1/quota/chat",
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
        )
        self.assertEqual(r.status_code, 405)

    # ── /v1/quota ────────────────────────────────────────────────────────────

    def test_quota_cookie_without_csrf_returns_403(self):
        self._login()
        r = self.client.post("/v1/quota")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"]["code"], "csrf_failed")

    def test_quota_bearer_returns_non_401(self):
        with patch.object(
            main_mod, "_fetch_quota",
            new=AsyncMock(return_value={
                "mode": "auto", "remaining": 10, "total": 50,
                "used": 40, "used_pct": 80.0, "window_seconds": 7200,
            }),
        ):
            r = self.client.post(
                "/v1/quota",
                headers={"Authorization": f"Bearer {TEST_API_KEY}"},
            )
        self.assertNotEqual(r.status_code, 401)
        self.assertNotEqual(r.status_code, 403)

    def test_quota_old_get_returns_405(self):
        r = self.client.get(
            "/v1/quota",
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
        )
        self.assertEqual(r.status_code, 405)

    # ── /admin/models/verify ─────────────────────────────────────────────────

    def test_models_verify_cookie_without_csrf_returns_403(self):
        self._login()
        r = self.client.post("/admin/models/verify", json={"mode_id": "fast"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"]["code"], "csrf_failed")

    def test_models_verify_bearer_returns_non_401(self):
        with patch.object(
            main_mod, "query_rate_limits",
            new=AsyncMock(return_value=_FAKE_QUOTA_RESPONSE),
        ):
            r = self.client.post(
                "/admin/models/verify",
                json={"mode_id": "fast"},
                headers={"Authorization": f"Bearer {TEST_API_KEY}"},
            )
        self.assertNotEqual(r.status_code, 401)
        self.assertNotEqual(r.status_code, 403)
        body = r.json()
        self.assertTrue(body.get("ok"))
        self.assertEqual(body["mode_id"], "fast")

    def test_models_verify_bearer_empty_mode_id_returns_400(self):
        r = self.client.post(
            "/admin/models/verify",
            json={"mode_id": ""},
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
        )
        self.assertEqual(r.status_code, 400)

    def test_models_verify_old_get_returns_405(self):
        r = self.client.get(
            "/admin/models/verify",
            params={"mode_id": "fast"},
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
        )
        self.assertEqual(r.status_code, 405)

    # ── /admin/dashboard ─────────────────────────────────────────────────────

    def test_dashboard_cookie_without_csrf_returns_403(self):
        self._login()
        r = self.client.post("/admin/dashboard")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"]["code"], "csrf_failed")

    def test_dashboard_bearer_returns_non_401(self):
        with (
            patch.object(
                main_mod, "query_image_rate_limits",
                new=AsyncMock(return_value=_FAKE_IMAGE_RATE_LIMITS),
            ),
            patch.object(
                main_mod, "_fetch_quota",
                new=AsyncMock(return_value={
                    "mode": "auto", "remaining": 10, "total": 50,
                    "used": 40, "used_pct": 80.0, "window_seconds": 7200,
                }),
            ),
        ):
            r = self.client.post(
                "/admin/dashboard",
                headers={"Authorization": f"Bearer {TEST_API_KEY}"},
            )
        self.assertNotEqual(r.status_code, 401)
        self.assertNotEqual(r.status_code, 403)

    def test_dashboard_old_get_returns_405(self):
        r = self.client.get(
            "/admin/dashboard",
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
        )
        self.assertEqual(r.status_code, 405)


class SideEffectEndpointWithCsrfTests(unittest.TestCase):
    """cookie 通道带正确 CSRF token 时端点能通过鉴权（进入业务逻辑）。"""

    @classmethod
    def setUpClass(cls) -> None:
        _override_settings(TEST_API_KEY)
        cls.client = TestClient(main_mod.app)

    def setUp(self) -> None:
        session_store.revoke_all()
        self.client.cookies.clear()
        _override_settings(TEST_API_KEY)

    def test_quota_image_cookie_with_csrf_passes_auth(self):
        login = self.client.post("/v1/auth/login", data={"api_key": TEST_API_KEY})
        csrf = login.json()["csrf_token"]
        with patch.object(
            main_mod, "query_image_rate_limits",
            new=AsyncMock(return_value=_FAKE_IMAGE_RATE_LIMITS),
        ):
            r = self.client.post(
                "/v1/quota/image",
                headers={"X-CSRF-Token": csrf},
            )
        # 不是 401/403 → 鉴权 + CSRF 校验均通过
        self.assertNotIn(r.status_code, (401, 403))

    def test_quota_cookie_with_csrf_passes_auth(self):
        login = self.client.post("/v1/auth/login", data={"api_key": TEST_API_KEY})
        csrf = login.json()["csrf_token"]
        with patch.object(
            main_mod, "_fetch_quota",
            new=AsyncMock(return_value={
                "mode": "auto", "remaining": 10, "total": 50,
                "used": 40, "used_pct": 80.0, "window_seconds": 7200,
            }),
        ):
            r = self.client.post(
                "/v1/quota",
                headers={"X-CSRF-Token": csrf},
            )
        self.assertNotIn(r.status_code, (401, 403))


_FAKE_ASSETS_RESPONSE = {
    "assets": [],
    "nextPageToken": "",
}


class GrokAssetsEndpointCsrfTests(unittest.TestCase):
    """Wave 5：/v1/grok/assets 改为 POST 的 CSRF 防护回归测试。

    该端点风险最高：无路径参数、触发上游 Grok HTTP 调用、写 DB、消耗配额。
    攻击者只需诱导受害者加载带 cookie 的请求即可触发副作用。
    """

    @classmethod
    def setUpClass(cls) -> None:
        _override_settings(TEST_API_KEY)
        cls.client = TestClient(main_mod.app)

    def setUp(self) -> None:
        session_store.revoke_all()
        self.client.cookies.clear()
        _override_settings(TEST_API_KEY)

    def _login(self):
        """登录并返回 csrf_token。"""
        r = self.client.post("/v1/auth/login", data={"api_key": TEST_API_KEY})
        self.assertEqual(r.status_code, 200)
        return r.json()["csrf_token"]

    # ── CSRF 防护：cookie 通道无 token → 403 ─────────────────────────────────

    def test_grok_assets_cookie_without_csrf_returns_403(self):
        """cookie 通道 POST 无 X-CSRF-Token → 403（CSRF 校验失败）。"""
        self._login()
        r = self.client.post("/v1/grok/assets")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"]["code"], "csrf_failed")

    # ── Bearer 通道不受 CSRF 约束 ─────────────────────────────────────────────

    def test_grok_assets_bearer_returns_non_401(self):
        """Bearer header 通道不受 CSRF 约束，能进入业务逻辑。"""
        with patch.object(
            main_mod, "list_grok_assets",
            new=AsyncMock(return_value=_FAKE_ASSETS_RESPONSE),
        ):
            r = self.client.post(
                "/v1/grok/assets",
                headers={"Authorization": f"Bearer {TEST_API_KEY}"},
            )
        self.assertNotEqual(r.status_code, 401)
        self.assertNotEqual(r.status_code, 403)

    # ── 旧 GET 路由已不存在 ────────────────────────────────────────────────────

    def test_grok_assets_old_get_returns_405(self):
        """旧 GET 路由已不存在，应返回 405 Method Not Allowed。"""
        r = self.client.get(
            "/v1/grok/assets",
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
        )
        self.assertEqual(r.status_code, 405)

    # ── cookie 通道带正确 CSRF token → 能通过鉴权 ────────────────────────────

    def test_grok_assets_cookie_with_csrf_passes_auth(self):
        """cookie 通道带正确 CSRF token 时能通过鉴权进入业务逻辑。"""
        csrf = self._login()
        with patch.object(
            main_mod, "list_grok_assets",
            new=AsyncMock(return_value=_FAKE_ASSETS_RESPONSE),
        ):
            r = self.client.post(
                "/v1/grok/assets",
                headers={"X-CSRF-Token": csrf},
            )
        self.assertNotIn(r.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
