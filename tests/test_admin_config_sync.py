"""admin/config 改凭证后自动同步 default 账号的集成测试。

验证：
1. POST /admin/config grok_cookie=new_val → default 账号 cookie 已更新
2. 只改 log_retention_days（非凭证字段）不触发同步
3. force_refresh_default 保留 enabled/priority/weight 及运行时状态
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
from mini_grok_api.accounts import Account, account_pool  # noqa: E402

TEST_API_KEY = "sync-test-key-xxxx"


def _override_settings(api_key: str = TEST_API_KEY):
    base = main_mod.settings_store.get()
    new = replace(base, api_key=api_key)
    main_mod.settings_store._settings = new  # type: ignore[attr-defined]
    return new


def _headers() -> dict:
    return {"Authorization": f"Bearer {TEST_API_KEY}"}


def _setup_pool() -> None:
    for info in account_pool.list_accounts():
        account_pool.delete_account(info.label)


class AdminConfigSyncTests(unittest.TestCase):
    """POST /admin/config 凭证同步测试。"""

    @classmethod
    def setUpClass(cls) -> None:
        _override_settings(TEST_API_KEY)
        cls.client = TestClient(main_mod.app)

    def setUp(self) -> None:
        _setup_pool()

    def test_admin_config_grok_cookie_change_syncs_default_account(self) -> None:
        """admin/config POST grok_cookie=new_val → default 账号 cookie 已更新。"""
        # 先植入 default 账号（模拟 lifespan import_from_settings）
        account_pool.upsert_account(Account(
            label="default",
            cookie="sso=old_cookie",
            user_agent="old-ua",
            browser="chrome142",
            proxy="",
            statsig_id="",
            enabled=True,
            priority=1,
            weight=10,
        ))

        old_acct = account_pool.get_account("default")
        self.assertIsNotNone(old_acct)
        self.assertEqual(old_acct.cookie, "sso=old_cookie")  # type: ignore[union-attr]

        # POST admin/config 修改 grok_cookie
        r = self.client.post(
            "/admin/config",
            headers=_headers(),
            data={"grok_cookie": "sso=new_cookie_value"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

        # default 账号 cookie 应已更新
        updated = account_pool.get_account("default")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.cookie, "sso=new_cookie_value")  # type: ignore[union-attr]

    def test_admin_config_no_credential_change_no_cookie_update(self) -> None:
        """只改 log_retention_days（非凭证字段）不触发 default 账号 cookie 同步。"""
        # 先植入 default 账号
        account_pool.upsert_account(Account(
            label="default",
            cookie="sso=stable_cookie",
            user_agent="stable-ua",
            browser="chrome142",
            proxy="",
            statsig_id="",
            enabled=True,
            priority=1,
            weight=10,
        ))

        # 同时确保 settings.grok_cookie 与账号不同（避免 force_refresh 意外覆盖）
        base = main_mod.settings_store.get()
        main_mod.settings_store._settings = replace(base, grok_cookie="sso=settings_cookie")  # type: ignore[attr-defined]

        try:
            # POST admin/config 仅修改 log_retention_days
            r = self.client.post(
                "/admin/config",
                headers=_headers(),
                data={"log_retention_days": "60"},
            )
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["ok"])

            # default 账号 cookie 不应变化
            acct = account_pool.get_account("default")
            self.assertIsNotNone(acct)
            self.assertEqual(acct.cookie, "sso=stable_cookie")  # type: ignore[union-attr]
        finally:
            main_mod.settings_store._settings = base  # type: ignore[attr-defined]

    def test_import_from_settings_force_refresh_preserves_runtime_state(self) -> None:
        """force_refresh_default=True 更新凭证但保留 enabled/priority/weight 和运行时状态。"""
        import dataclasses
        from mini_grok_api.config import load_settings

        # 植入 default 账号（priority=3, weight=5, enabled=False）
        account_pool.upsert_account(Account(
            label="default",
            cookie="sso=before",
            user_agent="",
            browser="chrome142",
            proxy="",
            statsig_id="",
            enabled=False,
            priority=3,
            weight=5,
        ))

        # 触发 force_refresh_default 同步
        base = load_settings()
        fresh = dataclasses.replace(base, grok_cookie="sso=after")
        result = account_pool.import_from_settings(fresh, force_refresh_default=True)
        self.assertTrue(result)

        # 凭证更新
        updated = account_pool.get_account("default")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.cookie, "sso=after")  # type: ignore[union-attr]

        # 配置字段保留
        self.assertEqual(updated.enabled, False)   # type: ignore[union-attr]
        self.assertEqual(updated.priority, 3)      # type: ignore[union-attr]
        self.assertEqual(updated.weight, 5)        # type: ignore[union-attr]

    def test_import_from_settings_force_refresh_creates_if_not_exists(self) -> None:
        """force_refresh_default=True 时，若 default 不存在则新建（而非 skip）。"""
        import dataclasses
        from mini_grok_api.config import load_settings

        # 确保 default 不存在
        account_pool.delete_account("default")

        base = load_settings()
        fresh = dataclasses.replace(base, grok_cookie="sso=fresh_cookie")
        result = account_pool.import_from_settings(fresh, force_refresh_default=True)
        self.assertTrue(result)

        acct = account_pool.get_account("default")
        self.assertIsNotNone(acct)
        self.assertEqual(acct.cookie, "sso=fresh_cookie")  # type: ignore[union-attr]
        self.assertEqual(acct.priority, 1)  # type: ignore[union-attr]
        self.assertTrue(acct.enabled)       # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
