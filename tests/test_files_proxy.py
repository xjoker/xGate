"""CSRF 安全回归测试：/v1/files/proxy 同源 Referer/Origin 校验。

涵盖：
- Bearer 通道（无 session cookie）：不校验 Referer，直接放行进业务逻辑（非 403）
- Cookie 通道无 Referer/Origin → 403 + cross_origin_blocked
- Cookie 通道带 same-origin Referer → 校验通过（非 403）
- Cookie 通道带 cross-origin Referer → 403 + cross_origin_blocked
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
from mini_grok_api.auth_session import session_store  # noqa: E402
from mini_grok_api.config import Settings  # noqa: E402

TEST_API_KEY = "test-key-files-proxy"

# 测试用的合法 assets.grok.com URL
_PROXY_URL = "https://assets.grok.com/some/image.png"


def _override_settings(api_key: str = TEST_API_KEY, public_base_url: str = "") -> Settings:
    base = main_mod.settings_store.get()
    new = replace(base, api_key=api_key, grok_cookie="fake-cookie-for-test",
                  public_base_url=public_base_url)
    main_mod.settings_store._settings = new  # type: ignore[attr-defined]
    return new


class FilesProxyCsrfTests(unittest.TestCase):
    """GET /v1/files/proxy 的同源 Referer/Origin 校验测试。"""

    @classmethod
    def setUpClass(cls) -> None:
        _override_settings(TEST_API_KEY)
        cls.client = TestClient(main_mod.app)

    def setUp(self) -> None:
        session_store.revoke_all()
        self.client.cookies.clear()
        _override_settings(TEST_API_KEY)

    def _login(self) -> str:
        """登录，返回 csrf_token，并让 TestClient 保持 session cookie。"""
        r = self.client.post("/v1/auth/login", data={"api_key": TEST_API_KEY})
        self.assertEqual(r.status_code, 200)
        return r.json()["csrf_token"]

    # ── 测试 1：Bearer 通道，无 Referer → 应放行（不是 403）────────────────────

    def test_bearer_no_referer_is_not_blocked(self):
        """Bearer 通道不走 cookie，不检查 Referer；url 无效只返回上游错误，不是 403。"""
        # stream_grok_asset 会真的发请求；mock 掉避免网络依赖
        async def _fake_stream(settings, key):  # noqa: ANN001
            async def _gen():
                yield b"fake"
            return "image/png", _gen()

        with patch.object(main_mod, "stream_grok_asset", side_effect=_fake_stream):
            r = self.client.get(
                "/v1/files/proxy",
                params={"url": _PROXY_URL},
                headers={"Authorization": f"Bearer {TEST_API_KEY}"},
            )
        # 不是 403（Referer 校验层应通过）
        self.assertNotEqual(r.status_code, 403)

    # ── 测试 2：Cookie 通道，无 Referer/Origin → 403 cross_origin_blocked ───────

    def test_cookie_no_referer_returns_403(self):
        """cookie 通道无 Referer 且无 Origin → 403 + cross_origin_blocked。"""
        self._login()
        # TestClient 默认不带 Referer；确保两个头都不存在
        r = self.client.get(
            "/v1/files/proxy",
            params={"url": _PROXY_URL},
            # 不传任何 Referer/Origin header
        )
        self.assertEqual(r.status_code, 403)
        body = r.json()
        self.assertEqual(body["error"]["code"], "cross_origin_blocked")

    # ── 测试 3：Cookie 通道，same-origin Referer → 校验通过（非 403）────────────

    def test_cookie_same_origin_referer_passes(self):
        """cookie 通道携带同源 Referer → 校验通过，进入业务逻辑（非 403）。"""
        self._login()

        async def _fake_stream(settings, key):  # noqa: ANN001
            async def _gen():
                yield b"fake"
            return "image/png", _gen()

        with patch.object(main_mod, "stream_grok_asset", side_effect=_fake_stream):
            r = self.client.get(
                "/v1/files/proxy",
                params={"url": _PROXY_URL},
                # TestClient 默认 base_url 是 http://testserver
                headers={"Referer": "http://testserver/"},
            )
        self.assertNotEqual(r.status_code, 403)

    # ── 测试 4：Cookie 通道，cross-origin Referer → 403 cross_origin_blocked ────

    def test_cookie_cross_origin_referer_returns_403(self):
        """cookie 通道携带跨域 Referer → 403 + cross_origin_blocked。"""
        self._login()
        r = self.client.get(
            "/v1/files/proxy",
            params={"url": _PROXY_URL},
            headers={"Referer": "https://evil.example.com/attack"},
        )
        self.assertEqual(r.status_code, 403)
        body = r.json()
        self.assertEqual(body["error"]["code"], "cross_origin_blocked")

    # ── 测试 5：Cookie 通道，same-origin Origin header → 校验通过 ───────────────

    def test_cookie_same_origin_origin_header_passes(self):
        """cookie 通道携带同源 Origin header → 校验通过（非 403）。"""
        self._login()

        async def _fake_stream(settings, key):  # noqa: ANN001
            async def _gen():
                yield b"fake"
            return "image/png", _gen()

        with patch.object(main_mod, "stream_grok_asset", side_effect=_fake_stream):
            r = self.client.get(
                "/v1/files/proxy",
                params={"url": _PROXY_URL},
                headers={"Origin": "http://testserver"},
            )
        self.assertNotEqual(r.status_code, 403)

    # ── 测试 6：Cookie 通道，public_base_url 配置时外部域名的同源 Referer ────────

    def test_cookie_public_base_url_referer_passes(self):
        """public_base_url 配置了反代域名时，该域名的 Referer 也应放行。"""
        _override_settings(TEST_API_KEY, public_base_url="https://xgate.example.com")
        self._login()

        async def _fake_stream(settings, key):  # noqa: ANN001
            async def _gen():
                yield b"fake"
            return "image/png", _gen()

        with patch.object(main_mod, "stream_grok_asset", side_effect=_fake_stream):
            r = self.client.get(
                "/v1/files/proxy",
                params={"url": _PROXY_URL},
                headers={"Referer": "https://xgate.example.com/gallery"},
            )
        self.assertNotEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
