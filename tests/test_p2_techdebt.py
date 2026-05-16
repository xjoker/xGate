"""P2 技术债清理回归测试 (v0.3.9)。

- P2-2: trust_x_forwarded_for 自定义 key_func
- P2-3: image quota hint 持久化到 settings.grok_image_quota_model_name
"""

from __future__ import annotations

import os
import pathlib
import unittest
from dataclasses import replace
from unittest.mock import MagicMock

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(_REPO_ROOT)

from mini_grok_api import main as main_mod  # noqa: E402


class TrustXForwardedForTests(unittest.TestCase):
    """P2-2: _client_ip_for_rate_limit 在 trust_x_forwarded_for 开关下行为正确。"""

    def _req(self, *, xff: str | None, client_host: str = "1.2.3.4"):
        m = MagicMock()
        m.headers = {"x-forwarded-for": xff} if xff is not None else {}
        m.client = MagicMock(host=client_host)
        return m

    def test_off_default_uses_client_host(self):
        base = main_mod.settings_store.get()
        main_mod.settings_store._settings = replace(base, trust_x_forwarded_for=False)
        ip = main_mod._client_ip_for_rate_limit(self._req(xff="9.9.9.9", client_host="1.2.3.4"))
        self.assertEqual(ip, "1.2.3.4")  # XFF 被忽略

    def test_on_uses_xff_first_ip(self):
        base = main_mod.settings_store.get()
        main_mod.settings_store._settings = replace(base, trust_x_forwarded_for=True)
        ip = main_mod._client_ip_for_rate_limit(self._req(xff="9.9.9.9, 10.10.10.10, 11.11.11.11"))
        self.assertEqual(ip, "9.9.9.9")  # 取最左 client

    def test_on_empty_xff_falls_back_to_client_host(self):
        base = main_mod.settings_store.get()
        main_mod.settings_store._settings = replace(base, trust_x_forwarded_for=True)
        ip = main_mod._client_ip_for_rate_limit(self._req(xff="", client_host="2.3.4.5"))
        self.assertEqual(ip, "2.3.4.5")

    def test_on_missing_xff_header_falls_back_to_client_host(self):
        base = main_mod.settings_store.get()
        main_mod.settings_store._settings = replace(base, trust_x_forwarded_for=True)
        ip = main_mod._client_ip_for_rate_limit(self._req(xff=None, client_host="3.3.3.3"))
        self.assertEqual(ip, "3.3.3.3")


class ImageQuotaHintPersistenceTests(unittest.TestCase):
    """P2-3: _persist_image_quota_hint 写 settings 字段。"""

    def test_persist_writes_settings(self):
        # 预设空值
        base = main_mod.settings_store.get()
        main_mod.settings_store._settings = replace(base, grok_image_quota_model_name="")
        main_mod._persist_image_quota_hint("aurora")
        self.assertEqual(main_mod.settings_store.get().grok_image_quota_model_name, "aurora")

    def test_persist_skips_when_same_value(self):
        """同值不重复写，避免每次 poll 都打盘。"""
        base = main_mod.settings_store.get()
        main_mod.settings_store._settings = replace(base, grok_image_quota_model_name="aurora")
        with unittest.mock.patch.object(main_mod.settings_store, "update") as mock_update:
            main_mod._persist_image_quota_hint("aurora")
            mock_update.assert_not_called()

    def test_persist_skips_empty(self):
        base = main_mod.settings_store.get()
        main_mod.settings_store._settings = replace(base, grok_image_quota_model_name="aurora")
        with unittest.mock.patch.object(main_mod.settings_store, "update") as mock_update:
            main_mod._persist_image_quota_hint("")
            mock_update.assert_not_called()


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
