"""集成测试：conversation_id → account_label sticky binding (0.3.2)。

覆盖：
- AccountPool.set / get / cleanup / list 单元路径
- _extract_conversation_id helper：metadata.conversation_id > user
- _persist_conversation_binding：跳过 fallback 伪 label / 空 conv_id
- /admin/conversation-bindings 端点
"""

from __future__ import annotations

import os
import pathlib
import time
import unittest
from dataclasses import replace
from unittest.mock import MagicMock

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(_REPO_ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from mini_grok_api import main as main_mod  # noqa: E402
from mini_grok_api.accounts import account_pool  # noqa: E402

TEST_API_KEY = "conv-binding-test"


def _override_settings():
    base = main_mod.settings_store.get()
    new = replace(base, api_key=TEST_API_KEY)
    main_mod.settings_store._settings = new  # type: ignore[attr-defined]


def _headers() -> dict:
    return {"Authorization": f"Bearer {TEST_API_KEY}"}


def _clear_bindings() -> None:
    """直接清空 conversation_account_map（测试隔离）。"""
    with account_pool._connect() as conn:
        conn.execute("DELETE FROM conversation_account_map")


class AccountPoolConversationBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        _clear_bindings()

    def test_get_returns_none_when_unbound(self):
        self.assertIsNone(account_pool.get_conversation_binding("conv-fresh"))

    def test_set_and_get_roundtrip(self):
        account_pool.set_conversation_binding("conv-1", "acc-A")
        self.assertEqual(account_pool.get_conversation_binding("conv-1"), "acc-A")

    def test_set_overwrites_existing(self):
        account_pool.set_conversation_binding("conv-2", "acc-A")
        account_pool.set_conversation_binding("conv-2", "acc-B")
        self.assertEqual(account_pool.get_conversation_binding("conv-2"), "acc-B")

    def test_empty_inputs_are_noops(self):
        account_pool.set_conversation_binding("", "acc-A")
        account_pool.set_conversation_binding("conv-x", "")
        self.assertIsNone(account_pool.get_conversation_binding(""))
        self.assertIsNone(account_pool.get_conversation_binding("conv-x"))

    def test_cleanup_prunes_expired_only(self):
        account_pool.set_conversation_binding("conv-fresh", "acc-A")
        # 手动把一个 binding 的 last_seen 推到过去（TTL+1s 前）
        with account_pool._connect() as conn:
            conn.execute(
                "INSERT INTO conversation_account_map "
                "(conversation_id, account_label, created_at, last_seen) "
                "VALUES (?, ?, ?, ?)",
                ("conv-stale", "acc-B", 0.0, time.time() - account_pool.CONVERSATION_TTL_SECONDS - 1),
            )
        deleted = account_pool.cleanup_old_conversation_bindings()
        self.assertEqual(deleted, 1)
        # fresh 仍在
        self.assertEqual(account_pool.get_conversation_binding("conv-fresh"), "acc-A")
        # stale 已删（即便用 raw query 也读不到）
        with account_pool._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM conversation_account_map WHERE conversation_id=?",
                ("conv-stale",),
            ).fetchone()
        self.assertIsNone(row)

    def test_list_returns_recent_first(self):
        account_pool.set_conversation_binding("conv-old", "acc-X")
        time.sleep(0.01)
        account_pool.set_conversation_binding("conv-new", "acc-Y")
        bindings = account_pool.list_conversation_bindings(limit=10)
        labels_in_order = [b["conversation_id"] for b in bindings]
        self.assertEqual(labels_in_order[0], "conv-new")

    def test_delete_conversation_binding_returns_true_when_present(self):
        """v0.3.6 BUG-G: delete_conversation_binding API"""
        account_pool.set_conversation_binding("conv-del", "acc-x")
        self.assertTrue(account_pool.delete_conversation_binding("conv-del"))
        self.assertIsNone(account_pool.get_conversation_binding("conv-del"))

    def test_delete_conversation_binding_returns_false_when_missing(self):
        self.assertFalse(account_pool.delete_conversation_binding("never-existed"))
        self.assertFalse(account_pool.delete_conversation_binding(""))

    def test_delete_account_cascades_to_binding(self):
        """SAST round 4 P3: delete_account 同事务清理 conversation_account_map。"""
        from mini_grok_api.accounts import Account
        account_pool.upsert_account(Account(
            label="acc-to-purge", cookie="sso=x", user_agent="", browser="chrome142",
            proxy="", statsig_id="", enabled=True, priority=1, weight=10,
        ))
        account_pool.set_conversation_binding("conv-bound-to-purge", "acc-to-purge")
        # 同时绑定到另一个 label 的 binding 不该被影响
        account_pool.set_conversation_binding("conv-other", "other-label")
        # 删账号
        self.assertTrue(account_pool.delete_account("acc-to-purge"))
        # 它的 binding 应该没了
        self.assertIsNone(account_pool.get_conversation_binding("conv-bound-to-purge"))
        # 其他 label 的 binding 不受影响
        self.assertEqual(account_pool.get_conversation_binding("conv-other"), "other-label")


class ExtractConversationIdTests(unittest.TestCase):
    def _req(self, **kwargs):
        m = MagicMock()
        m.metadata = kwargs.get("metadata", None)
        m.user = kwargs.get("user", None)
        return m

    def test_metadata_conversation_id_wins(self):
        r = self._req(metadata={"conversation_id": "conv-from-meta"}, user="user-fallback")
        self.assertEqual(main_mod._extract_conversation_id(r), "conv-from-meta")

    def test_metadata_camelcase_supported(self):
        r = self._req(metadata={"conversationId": "conv-camel"})
        self.assertEqual(main_mod._extract_conversation_id(r), "conv-camel")

    def test_user_field_fallback(self):
        r = self._req(metadata=None, user="user-as-conv")
        self.assertEqual(main_mod._extract_conversation_id(r), "user-as-conv")

    def test_no_source_returns_none(self):
        r = self._req(metadata=None, user=None)
        self.assertIsNone(main_mod._extract_conversation_id(r))

    def test_long_id_truncated_to_256(self):
        r = self._req(user="x" * 1000)
        self.assertEqual(len(main_mod._extract_conversation_id(r)), 256)


class PersistConversationBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        _clear_bindings()

    def test_persists_normal_label(self):
        main_mod._persist_conversation_binding("conv-good", "acc-real")
        self.assertEqual(account_pool.get_conversation_binding("conv-good"), "acc-real")

    def test_skips_fallback_label(self):
        # _settings_fallback / 其他以 _ 开头的伪 label 不应该写入
        main_mod._persist_conversation_binding("conv-fb", "_settings_fallback")
        self.assertIsNone(account_pool.get_conversation_binding("conv-fb"))

    def test_skips_missing_conv_id(self):
        main_mod._persist_conversation_binding(None, "acc-x")
        # 不应抛错；表里也不应有空 conv_id 行
        with account_pool._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM conversation_account_map").fetchone()
        self.assertEqual(row[0], 0)


class AdminConversationBindingsEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _override_settings()
        cls.client = TestClient(main_mod.app)

    def setUp(self) -> None:
        _clear_bindings()

    def test_endpoint_lists_bindings(self):
        account_pool.set_conversation_binding("conv-A", "acc-1")
        account_pool.set_conversation_binding("conv-B", "acc-2")
        r = self.client.get("/admin/conversation-bindings", headers=_headers())
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("bindings", body)
        self.assertIn("ttl_seconds", body)
        self.assertEqual(body["ttl_seconds"], 7 * 86400)
        conv_ids = {b["conversation_id"] for b in body["bindings"]}
        self.assertEqual(conv_ids, {"conv-A", "conv-B"})

    def test_endpoint_requires_auth(self):
        r = self.client.get("/admin/conversation-bindings")
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
