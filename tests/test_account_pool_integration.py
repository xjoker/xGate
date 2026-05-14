"""账号池 Admin Endpoints 集成测试。

通过 FastAPI TestClient 以 HTTP 方式验证 admin/accounts 端点的完整行为，
包括新增、删除、启用/禁用、cURL 导入。

不依赖真实 Grok 网络调用，所有上游 HTTP 均已 mock 或不需调用。
"""

from __future__ import annotations

import os
import pathlib
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(_REPO_ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from mini_grok_api import main as main_mod  # noqa: E402
from mini_grok_api.accounts import Account, AccountPool, account_pool  # noqa: E402

TEST_API_KEY = "acct-test-key-xxxx"

_GROK_CURL = (
    "curl 'https://grok.com/rest/app-chat/conversations' "
    "--compressed "
    "-H 'accept: */*' "
    "-H 'accept-language: zh-CN,zh;q=0.9' "
    "-H 'cookie: sso=abc123; sso-rw=def456' "
    "-H 'user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36'"
)


def _override_settings(api_key: str = TEST_API_KEY):
    """替换 settings_store 中的 api_key（不落盘）。"""
    from mini_grok_api.config import Settings
    base = main_mod.settings_store.get()
    new = replace(base, api_key=api_key)
    main_mod.settings_store._settings = new  # type: ignore[attr-defined]
    return new


def _headers() -> dict:
    return {"Authorization": f"Bearer {TEST_API_KEY}"}


def _setup_pool() -> None:
    """清空账号池，确保测试隔离。"""
    with account_pool._lock:
        account_pool._accounts.clear()


class AdminAccountsTests(unittest.TestCase):
    """Admin /admin/accounts 端点集成测试。"""

    @classmethod
    def setUpClass(cls) -> None:
        _override_settings(TEST_API_KEY)
        cls.client = TestClient(main_mod.app)

    def setUp(self) -> None:
        _setup_pool()

    # ------------------------------------------------------------------
    # GET /admin/accounts
    # ------------------------------------------------------------------

    def test_list_accounts_empty(self) -> None:
        """初始状态下列表为空。"""
        r = self.client.get("/admin/accounts", headers=_headers())
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("accounts", body)
        self.assertEqual(body["accounts"], [])

    def test_list_accounts_after_upsert(self) -> None:
        """新增账号后列表多一条。"""
        self.client.post("/admin/accounts", headers=_headers(), json={
            "label": "test-acct",
            "cookie": "sso=hello123; sso-rw=world456",
        })
        r = self.client.get("/admin/accounts", headers=_headers())
        self.assertEqual(r.status_code, 200)
        accounts = r.json()["accounts"]
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["label"], "test-acct")

    def test_list_accounts_requires_auth(self) -> None:
        """未认证时返回 401。"""
        r = self.client.get("/admin/accounts")
        self.assertEqual(r.status_code, 401)

    # ------------------------------------------------------------------
    # POST /admin/accounts （upsert）
    # ------------------------------------------------------------------

    def test_upsert_creates_new_account(self) -> None:
        """POST 新建账号并返回 ok=True。"""
        r = self.client.post("/admin/accounts", headers=_headers(), json={
            "label": "acc-alpha",
            "cookie": "sso=aaa; sso-rw=bbb",
            "priority": 2,
            "weight": 20,
        })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["label"], "acc-alpha")

    def test_upsert_creates_then_list_shows_entry(self) -> None:
        """新增后 list 能查到。"""
        self.client.post("/admin/accounts", headers=_headers(), json={
            "label": "acc-beta",
            "cookie": "sso=xyz; sso-rw=abc",
        })
        r = self.client.get("/admin/accounts", headers=_headers())
        labels = [a["label"] for a in r.json()["accounts"]]
        self.assertIn("acc-beta", labels)

    def test_upsert_updates_existing_account(self) -> None:
        """相同 label 第二次 POST 应覆盖（不新增重复条目）。"""
        for cookie in ("sso=first", "sso=second"):
            self.client.post("/admin/accounts", headers=_headers(), json={
                "label": "acc-dup",
                "cookie": cookie,
            })
        r = self.client.get("/admin/accounts", headers=_headers())
        accts = [a for a in r.json()["accounts"] if a["label"] == "acc-dup"]
        self.assertEqual(len(accts), 1)

    def test_upsert_empty_label_returns_400(self) -> None:
        r = self.client.post("/admin/accounts", headers=_headers(), json={
            "label": "",
            "cookie": "sso=xxx",
        })
        self.assertEqual(r.status_code, 400)

    def test_upsert_empty_cookie_returns_400(self) -> None:
        r = self.client.post("/admin/accounts", headers=_headers(), json={
            "label": "acc-no-cookie",
            "cookie": "",
        })
        self.assertEqual(r.status_code, 400)

    # ------------------------------------------------------------------
    # DELETE /admin/accounts/{label}
    # ------------------------------------------------------------------

    def test_delete_account_removes_it(self) -> None:
        """DELETE 后账号从列表消失。"""
        self.client.post("/admin/accounts", headers=_headers(), json={
            "label": "acc-to-delete",
            "cookie": "sso=del; sso-rw=del2",
        })
        r = self.client.delete("/admin/accounts/acc-to-delete", headers=_headers())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

        r2 = self.client.get("/admin/accounts", headers=_headers())
        labels = [a["label"] for a in r2.json()["accounts"]]
        self.assertNotIn("acc-to-delete", labels)

    def test_delete_nonexistent_account_returns_404(self) -> None:
        r = self.client.delete("/admin/accounts/does-not-exist", headers=_headers())
        self.assertEqual(r.status_code, 404)

    # ------------------------------------------------------------------
    # POST /admin/accounts/{label}/enabled
    # ------------------------------------------------------------------

    def test_disable_account_changes_status(self) -> None:
        """禁用后账号 status 变为 manually_disabled。"""
        self.client.post("/admin/accounts", headers=_headers(), json={
            "label": "acc-toggle",
            "cookie": "sso=abc; sso-rw=def",
        })
        r = self.client.post(
            "/admin/accounts/acc-toggle/enabled",
            headers=_headers(),
            json={"enabled": False},
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertFalse(r.json()["enabled"])

        # 从列表验证 status
        accts = self.client.get("/admin/accounts", headers=_headers()).json()["accounts"]
        target = next((a for a in accts if a["label"] == "acc-toggle"), None)
        self.assertIsNotNone(target)
        self.assertEqual(target["status"], "manually_disabled")

    def test_enable_account_restores_status(self) -> None:
        """先禁用再启用，status 恢复为 enabled。"""
        self.client.post("/admin/accounts", headers=_headers(), json={
            "label": "acc-restore",
            "cookie": "sso=abc; sso-rw=def",
        })
        self.client.post(
            "/admin/accounts/acc-restore/enabled",
            headers=_headers(),
            json={"enabled": False},
        )
        self.client.post(
            "/admin/accounts/acc-restore/enabled",
            headers=_headers(),
            json={"enabled": True},
        )
        accts = self.client.get("/admin/accounts", headers=_headers()).json()["accounts"]
        target = next((a for a in accts if a["label"] == "acc-restore"), None)
        self.assertIsNotNone(target)
        self.assertEqual(target["status"], "enabled")

    def test_set_enabled_nonexistent_returns_404(self) -> None:
        r = self.client.post(
            "/admin/accounts/ghost-account/enabled",
            headers=_headers(),
            json={"enabled": False},
        )
        self.assertEqual(r.status_code, 404)

    # ------------------------------------------------------------------
    # POST /admin/accounts/import-curl
    # ------------------------------------------------------------------

    def test_import_curl_creates_account(self) -> None:
        """从 cURL 导入应创建新账号。"""
        r = self.client.post(
            "/admin/accounts/import-curl",
            headers=_headers(),
            json={
                "curl": _GROK_CURL,
                "label": "curl-imported",
                "priority": 1,
                "weight": 5,
            },
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["label"], "curl-imported")
        self.assertIn("browser", body)

        # 验证 list 中有该账号
        accts = self.client.get("/admin/accounts", headers=_headers()).json()["accounts"]
        labels = [a["label"] for a in accts]
        self.assertIn("curl-imported", labels)

    def test_import_curl_invalid_curl_returns_400(self) -> None:
        r = self.client.post(
            "/admin/accounts/import-curl",
            headers=_headers(),
            json={
                "curl": "这不是一个 curl 命令",
                "label": "bad-curl-account",
            },
        )
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertFalse(body.get("ok", True))
        self.assertIn("error", body)

    def test_import_curl_empty_label_returns_400(self) -> None:
        r = self.client.post(
            "/admin/accounts/import-curl",
            headers=_headers(),
            json={"curl": _GROK_CURL, "label": ""},
        )
        self.assertEqual(r.status_code, 400)

    def test_import_curl_empty_curl_returns_400(self) -> None:
        r = self.client.post(
            "/admin/accounts/import-curl",
            headers=_headers(),
            json={"curl": "", "label": "some-label"},
        )
        self.assertEqual(r.status_code, 400)

    # ------------------------------------------------------------------
    # 多账号操作
    # ------------------------------------------------------------------

    def test_multiple_accounts_list_sorted(self) -> None:
        """多个账号按 priority asc 排序。"""
        for label, priority in [("z-acc", 3), ("a-acc", 1), ("m-acc", 2)]:
            self.client.post("/admin/accounts", headers=_headers(), json={
                "label": label,
                "cookie": f"sso={label}",
                "priority": priority,
            })
        accts = self.client.get("/admin/accounts", headers=_headers()).json()["accounts"]
        priorities = [a["priority"] for a in accts]
        self.assertEqual(priorities, sorted(priorities))

    def test_account_info_contains_expected_fields(self) -> None:
        """账号信息包含所有期望字段。"""
        self.client.post("/admin/accounts", headers=_headers(), json={
            "label": "field-check",
            "cookie": "sso=hello; sso-rw=world",
            "priority": 1,
            "weight": 15,
        })
        accts = self.client.get("/admin/accounts", headers=_headers()).json()["accounts"]
        acc = next((a for a in accts if a["label"] == "field-check"), None)
        self.assertIsNotNone(acc)
        expected_fields = [
            "label", "enabled", "priority", "weight", "status",
            "cooldown_until", "last_used_at", "last_error_code",
            "last_error_at", "consecutive_failures", "success_count",
            "fail_count", "cookie_masked",
        ]
        for f in expected_fields:
            self.assertIn(f, acc, f"字段 {f!r} 不存在于 AccountInfo")

    def test_cookie_is_masked_in_list(self) -> None:
        """列表中 cookie 字段应该是掩码，不应暴露原始 cookie。"""
        raw_cookie = "sso=super_secret_token_abcdef1234567890; sso-rw=another_secret"
        self.client.post("/admin/accounts", headers=_headers(), json={
            "label": "mask-test",
            "cookie": raw_cookie,
        })
        accts = self.client.get("/admin/accounts", headers=_headers()).json()["accounts"]
        acc = next((a for a in accts if a["label"] == "mask-test"), None)
        self.assertIsNotNone(acc)
        cookie_masked = acc.get("cookie_masked", "")
        # 掩码后不应包含完整 cookie 值
        self.assertNotEqual(cookie_masked, raw_cookie)
        self.assertTrue(len(cookie_masked) < len(raw_cookie))


class AccountPoolFallbackTests(unittest.TestCase):
    """settings.grok_cookie fallback 测试。

    删光所有账号后，settings 中有值的情况下，service 仍可工作。
    通过 /health endpoint 验证 cookie_configured 字段。
    """

    @classmethod
    def setUpClass(cls) -> None:
        _override_settings(TEST_API_KEY)
        cls.client = TestClient(main_mod.app)

    def setUp(self) -> None:
        _setup_pool()

    def test_health_reflects_settings_cookie(self) -> None:
        """即使账号池为空，settings.grok_cookie 有值时 health 显示 cookie_configured=True。"""
        # 设置 settings 中的 grok_cookie
        base = main_mod.settings_store.get()
        main_mod.settings_store._settings = replace(  # type: ignore[attr-defined]
            base, grok_cookie="sso=fallback_cookie_value"
        )
        try:
            r = self.client.get("/health")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["cookie_configured"])
        finally:
            # 恢复
            main_mod.settings_store._settings = base  # type: ignore[attr-defined]

    def test_no_accounts_and_no_cookie(self) -> None:
        """账号池空 + settings.grok_cookie 空 → health.cookie_configured=False。"""
        base = main_mod.settings_store.get()
        main_mod.settings_store._settings = replace(  # type: ignore[attr-defined]
            base, grok_cookie=""
        )
        try:
            r = self.client.get("/health")
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.json()["cookie_configured"])
        finally:
            main_mod.settings_store._settings = base  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
