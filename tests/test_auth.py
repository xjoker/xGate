"""鉴权与 OpenAI 兼容端点集成测试。

用 FastAPI TestClient 启 ASGI（不会真的开端口），mock 上游 Grok 网络调用。
覆盖：
- 三通道鉴权：Bearer / x-api-key / HttpOnly cookie
- /v1/auth/login + /v1/auth/logout + /v1/auth/whoami
- CSRF Double Submit 校验
- /v1/models 不再要求登录失败时不放行
- 占位端点 /v1/embeddings 返回 501
- /v1/grok/assets/download 不再支持 ?api_key= query
- access log 脱敏过滤器
"""

from __future__ import annotations

import logging
import re
import unittest
from dataclasses import replace

# 确保 import main 之前 cwd 指向项目根（main.py 里依赖 cwd 找 data/config）。
import os
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(_REPO_ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from mini_grok_api import main as main_mod  # noqa: E402
from mini_grok_api.auth_session import (  # noqa: E402
    CSRF_COOKIE,
    SESSION_COOKIE,
    session_store,
)
from mini_grok_api.config import Settings  # noqa: E402

TEST_API_KEY = "test-key-xxxx-yyyy"


def _override_settings(api_key: str = TEST_API_KEY) -> Settings:
    base = main_mod.settings_store.get()
    new = replace(base, api_key=api_key)
    main_mod.settings_store._settings = new  # type: ignore[attr-defined]
    return new


class AuthChannelTests(unittest.TestCase):
    """三通道鉴权 + cookie session + CSRF。"""

    @classmethod
    def setUpClass(cls) -> None:
        _override_settings(TEST_API_KEY)
        cls.client = TestClient(main_mod.app)

    def setUp(self) -> None:
        # 每个 case 清空 session 表，避免 cookie 互相串
        session_store.revoke_all()

    # --- channel 1: Authorization: Bearer ---------------------------------

    def test_bearer_passes(self) -> None:
        r = self.client.get("/v1/models", headers={"Authorization": f"Bearer {TEST_API_KEY}"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["object"], "list")
        self.assertGreater(len(body["data"]), 0)

    def test_bearer_wrong_returns_401_authentication_error(self) -> None:
        r = self.client.get("/v1/models", headers={"Authorization": "Bearer WRONG"})
        self.assertEqual(r.status_code, 401)
        body = r.json()
        # FastAPI 把 detail 里的 dict 包成 detail 字段
        self.assertIn("detail", body)
        self.assertEqual(body["detail"]["type"], "authentication_error")

    # --- channel 2: x-api-key ---------------------------------------------

    def test_x_api_key_passes(self) -> None:
        r = self.client.get("/v1/models", headers={"X-Api-Key": TEST_API_KEY})
        self.assertEqual(r.status_code, 200)

    def test_x_api_key_wrong_fails(self) -> None:
        r = self.client.get("/v1/models", headers={"X-Api-Key": "nope"})
        self.assertEqual(r.status_code, 401)

    def test_no_credentials_returns_401(self) -> None:
        r = self.client.get("/v1/models")
        self.assertEqual(r.status_code, 401)

    # --- channel 3: HttpOnly Cookie session -------------------------------

    def test_login_sets_cookies_and_returns_csrf(self) -> None:
        r = self.client.post("/v1/auth/login", data={"api_key": TEST_API_KEY})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertIn("csrf_token", body)
        self.assertIn(SESSION_COOKIE, r.cookies)
        self.assertIn(CSRF_COOKIE, r.cookies)
        # CSRF cookie 必须前端 JS 可读 → HttpOnly=False
        # TestClient 没暴露 HttpOnly 标志，但至少能读到值
        self.assertEqual(r.cookies[CSRF_COOKIE], body["csrf_token"])

    def test_login_wrong_key_returns_401(self) -> None:
        r = self.client.post("/v1/auth/login", data={"api_key": "WRONG"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"]["type"], "authentication_error")

    def test_login_via_json_body(self) -> None:
        r = self.client.post("/v1/auth/login", json={"api_key": TEST_API_KEY})
        self.assertEqual(r.status_code, 200)

    def test_cookie_get_passes_without_csrf(self) -> None:
        # GET 不需要 CSRF 校验
        login = self.client.post("/v1/auth/login", data={"api_key": TEST_API_KEY})
        self.assertEqual(login.status_code, 200)
        r = self.client.get("/v1/models")
        self.assertEqual(r.status_code, 200)

    def test_cookie_post_without_csrf_returns_403(self) -> None:
        login = self.client.post("/v1/auth/login", data={"api_key": TEST_API_KEY})
        self.assertEqual(login.status_code, 200)
        # 手工不附 X-CSRF-Token 头，使用 /v1/auth/logout 触发 CSRF 校验
        # （logout 有独立的 CSRF 逻辑，session 有效时不带 csrf 返回 403）
        r = self.client.post("/v1/grok-files/sync-now")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"]["type"], "permission_error")
        self.assertEqual(r.json()["detail"]["code"], "csrf_failed")

    def test_cookie_post_with_correct_csrf_passes(self) -> None:
        login = self.client.post("/v1/auth/login", data={"api_key": TEST_API_KEY})
        csrf = login.json()["csrf_token"]
        r = self.client.post("/v1/grok-files/sync-now", headers={"X-CSRF-Token": csrf})
        # 能进得去说明鉴权 + csrf 都过了（sync-now 本身可能返回 200 或其他业务状态）
        self.assertNotEqual(r.status_code, 403)
        self.assertNotEqual(r.status_code, 401)

    def test_cookie_post_with_mismatched_csrf_fails(self) -> None:
        login = self.client.post("/v1/auth/login", data={"api_key": TEST_API_KEY})
        self.assertEqual(login.status_code, 200)
        # csrf cookie 由 TestClient 自动带；header 给一个不一样的
        r = self.client.post("/v1/grok-files/sync-now", headers={"X-CSRF-Token": "fake-csrf"})
        self.assertEqual(r.status_code, 403)

    def test_logout_without_csrf_is_blocked(self) -> None:
        # 持有有效 session 时，logout 也走 CSRF 校验，防 CSRF logout
        login = self.client.post("/v1/auth/login", data={"api_key": TEST_API_KEY})
        self.assertEqual(login.status_code, 200)
        r = self.client.post("/v1/auth/logout")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"]["code"], "csrf_failed")
        # session 仍然有效
        r2 = self.client.get("/v1/models")
        self.assertEqual(r2.status_code, 200)

    def test_logout_with_csrf_revokes_session(self) -> None:
        login = self.client.post("/v1/auth/login", data={"api_key": TEST_API_KEY})
        csrf = login.json()["csrf_token"]
        r = self.client.post("/v1/auth/logout", headers={"X-CSRF-Token": csrf})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["revoked"])
        r2 = self.client.get("/v1/models")
        self.assertEqual(r2.status_code, 401)

    def test_logout_without_session_is_idempotent(self) -> None:
        # 无 session（cookie 过期 / 已被撤销）时，logout 幂等放行只清 cookie
        self.client.cookies.clear()
        r = self.client.post("/v1/auth/logout")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["revoked"])

    def test_whoami_with_session(self) -> None:
        self.client.post("/v1/auth/login", data={"api_key": TEST_API_KEY})
        r = self.client.get("/v1/auth/whoami")
        self.assertEqual(r.status_code, 200)
        self.assertIn("csrf_token", r.json())

    def test_whoami_without_session(self) -> None:
        # 确保没有遗留 cookie
        self.client.cookies.clear()
        r = self.client.get("/v1/auth/whoami")
        self.assertEqual(r.status_code, 401)


class PlaceholderEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _override_settings(TEST_API_KEY)
        cls.client = TestClient(main_mod.app)

    def setUp(self) -> None:
        session_store.revoke_all()
        self.client.cookies.clear()

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {TEST_API_KEY}"}

    def test_embeddings_returns_404(self) -> None:
        # /v1/embeddings 路由已被有意移除，FastAPI 返回 404（Not Found）
        r = self.client.post("/v1/embeddings", headers=self._h())
        self.assertEqual(r.status_code, 404)

    def test_completions_returns_404(self) -> None:
        # /v1/completions 路由已被有意移除，FastAPI 返回 404（Not Found）
        r = self.client.post("/v1/completions", headers=self._h())
        self.assertEqual(r.status_code, 404)

    def test_audio_speech_returns_404(self) -> None:
        # /v1/audio/speech 路由已被有意移除，FastAPI 返回 404（Not Found）
        r = self.client.post("/v1/audio/speech", headers=self._h())
        self.assertEqual(r.status_code, 404)


class DownloadEndpointSecurityTests(unittest.TestCase):
    """确认 ?api_key= query 通道已被移除——传 query 不应放行。"""

    @classmethod
    def setUpClass(cls) -> None:
        _override_settings(TEST_API_KEY)
        cls.client = TestClient(main_mod.app)

    def setUp(self) -> None:
        session_store.revoke_all()
        self.client.cookies.clear()

    def test_query_api_key_no_longer_authenticates(self) -> None:
        # 这是 P1 TODO 的安全核心：query api_key 不应再放行。
        r = self.client.get(
            "/v1/grok/assets/download",
            params={"key": "users/something/file.png", "api_key": TEST_API_KEY},
        )
        # 没有 Header / cookie → 401
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["detail"]["type"], "authentication_error")

    def test_bearer_header_still_works(self) -> None:
        # 鉴权层应放行（不是 401）。mock 下游避免真去打 assets.grok.com。
        from mini_grok_api.grok_client import GrokClientError

        async def fake_stream(*args, **kwargs):
            raise GrokClientError("not found", status_code=404, code="not_found")

        original = main_mod.stream_grok_asset
        main_mod.stream_grok_asset = fake_stream  # type: ignore[assignment]
        try:
            r = self.client.get(
                "/v1/grok/assets/download",
                params={"key": "nonexistent"},
                headers={"Authorization": f"Bearer {TEST_API_KEY}"},
            )
        finally:
            main_mod.stream_grok_asset = original  # type: ignore[assignment]
        # 不能是 401（鉴权应放行）；具体 status 是 404（来自 mock）。
        self.assertNotEqual(r.status_code, 401)
        self.assertEqual(r.status_code, 404)


class ChatCompletionsTests(unittest.TestCase):
    """非真实调用 Grok：mock complete_chat / stream_chat。"""

    @classmethod
    def setUpClass(cls) -> None:
        _override_settings(TEST_API_KEY)
        cls.client = TestClient(main_mod.app)

    def setUp(self) -> None:
        session_store.revoke_all()
        self.client.cookies.clear()

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {TEST_API_KEY}"}

    def test_unknown_fields_do_not_cause_422(self) -> None:
        async def fake_complete(*args, **kwargs):
            return "ok"
        main_mod.complete_chat = fake_complete  # type: ignore[assignment]
        try:
            r = self.client.post("/v1/chat/completions", headers=self._h(), json={
                "model": "grok-4.20-auto",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                # 未知字段 + 已知未实现字段
                "totally_made_up": True,
                "frequency_penalty": 0.5,
                "user": "u-1",
                "tools": [{"type": "function", "function": {"name": "f"}}],
            })
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["object"], "chat.completion")
            self.assertEqual(body["choices"][0]["message"]["content"], "ok")
            self.assertIn("system_fingerprint", body)
        finally:
            pass

    def test_image_url_in_message_does_not_400(self) -> None:
        async def fake_complete(*args, **kwargs):
            return "ok"
        main_mod.complete_chat = fake_complete  # type: ignore[assignment]
        r = self.client.post("/v1/chat/completions", headers=self._h(), json={
            "model": "grok-4.20-auto",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "看图"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}},
                ],
            }],
        })
        self.assertEqual(r.status_code, 200)

    def test_unknown_model_returns_404_not_found_error(self) -> None:
        r = self.client.post("/v1/chat/completions", headers=self._h(), json={
            "model": "definitely-not-real",
            "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertEqual(r.status_code, 404)
        body = r.json()
        self.assertEqual(body["error"]["type"], "not_found_error")
        self.assertEqual(body["error"]["code"], "model_not_found")

    def test_stream_with_include_usage_emits_usage_chunk(self) -> None:
        from mini_grok_api.grok_client import GrokTextDelta as ChatDelta

        async def fake_stream(*args, **kwargs):
            for piece in ("hel", "lo"):
                yield ChatDelta(content=piece, done=False)
            yield ChatDelta(content="", done=True)

        main_mod.stream_chat = fake_stream  # type: ignore[assignment]
        with self.client.stream("POST", "/v1/chat/completions", headers=self._h(), json={
            "model": "grok-4.20-auto",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        }) as resp:
            self.assertEqual(resp.status_code, 200)
            text = b"".join(resp.iter_bytes()).decode("utf-8")

        # 必须有 usage chunk（在 [DONE] 之前）
        self.assertIn('"usage"', text)
        self.assertIn("[DONE]", text)
        # 第一个 delta 带 role=assistant
        self.assertIn('"role":"assistant"', text)

    def test_stream_max_tokens_truncates_before_yielding(self) -> None:
        """关键：max_tokens 截断必须发生在 yield 给客户端**之前**，不能让 client 看到超量内容。"""
        from mini_grok_api.grok_client import GrokTextDelta as ChatDelta

        async def fake_stream(*args, **kwargs):
            # 一次性吐 50 字符的 delta；max_tokens=1 → char_budget=4
            yield ChatDelta(content="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMN", done=False)
            yield ChatDelta(content="", done=True)

        main_mod.stream_chat = fake_stream  # type: ignore[assignment]
        with self.client.stream("POST", "/v1/chat/completions", headers=self._h(), json={
            "model": "grok-4.20-auto",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "max_tokens": 1,
        }) as resp:
            text = b"".join(resp.iter_bytes()).decode("utf-8")

        # 提取所有 delta.content 拼起来，必须严格 <= 4 字符（max_tokens=1 → 4 chars budget）
        import re as _re
        contents = _re.findall(r'"content":"([^"]*)"', text)
        emitted = "".join(c for c in contents)
        self.assertLessEqual(len(emitted), 4, f"emitted={emitted!r} should be truncated")
        self.assertGreater(len(emitted), 0, "should still emit something")
        # 最终 finish_reason 必须是 length
        self.assertIn('"finish_reason":"length"', text)

    def test_stream_without_include_usage_has_no_usage_chunk(self) -> None:
        from mini_grok_api.grok_client import GrokTextDelta as ChatDelta

        async def fake_stream(*args, **kwargs):
            yield ChatDelta(content="x", done=False)
            yield ChatDelta(content="", done=True)

        main_mod.stream_chat = fake_stream  # type: ignore[assignment]
        with self.client.stream("POST", "/v1/chat/completions", headers=self._h(), json={
            "model": "grok-4.20-auto",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }) as resp:
            text = b"".join(resp.iter_bytes()).decode("utf-8")
        self.assertNotIn('"usage"', text)
        self.assertIn("[DONE]", text)


class ApiKeyRotationTests(unittest.TestCase):
    """api_key 轮换必须撤销所有现存 session。"""

    @classmethod
    def setUpClass(cls) -> None:
        _override_settings(TEST_API_KEY)
        cls.client = TestClient(main_mod.app)

    def setUp(self) -> None:
        session_store.revoke_all()
        self.client.cookies.clear()
        _override_settings(TEST_API_KEY)

    def test_session_store_revoke_all(self) -> None:
        session_store.create()
        session_store.create()
        self.assertEqual(session_store.size(), 2)
        n = session_store.revoke_all()
        self.assertEqual(n, 2)
        self.assertEqual(session_store.size(), 0)

    def test_admin_config_api_key_change_revokes_all_sessions(self) -> None:
        # 1) 登录拿到 session
        login = self.client.post("/v1/auth/login", data={"api_key": TEST_API_KEY})
        csrf = login.json()["csrf_token"]
        # 用 cookie 通道访问受保护接口确认有效
        self.assertEqual(self.client.get("/v1/models").status_code, 200)
        self.assertGreaterEqual(session_store.size(), 1)

        # 2) 用 Bearer 通道（不依赖 cookie）改 api_key
        new_key = "rotated-key-zzzz"
        # 不真去落盘配置：mock save_settings 避免污染 data/config/mini.toml
        from mini_grok_api import config as cfg_mod
        original_save = cfg_mod.save_settings
        cfg_mod.save_settings = lambda settings, path=cfg_mod.CONFIG_PATH: None  # type: ignore[assignment]
        try:
            r = self.client.post(
                "/admin/config",
                data={"api_key": new_key, "grok_proxy": ""},
                headers={"Authorization": f"Bearer {TEST_API_KEY}"},
            )
            self.assertEqual(r.status_code, 200)
        finally:
            cfg_mod.save_settings = original_save  # type: ignore[assignment]

        # 3) 所有 session 应被清空
        self.assertEqual(session_store.size(), 0)
        # 旧 cookie 已无效
        self.assertEqual(self.client.get("/v1/models").status_code, 401)


class LogRedactionTests(unittest.TestCase):
    def test_filter_redacts_api_key_in_query(self) -> None:
        f = main_mod._SecretsRedactFilter()
        record = logging.LogRecord(
            name="uvicorn.access", level=logging.INFO, pathname="", lineno=0,
            msg="GET /v1/grok/assets/download?key=foo&api_key=SECRET HTTP/1.1 200",
            args=(), exc_info=None,
        )
        kept = f.filter(record)
        self.assertTrue(kept)
        self.assertIn("api_key=***", record.getMessage())
        self.assertNotIn("SECRET", record.getMessage())

    def test_filter_redacts_authorization_header(self) -> None:
        f = main_mod._SecretsRedactFilter()
        record = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="Authorization: Bearer abcdef-secret-token",
            args=(), exc_info=None,
        )
        f.filter(record)
        self.assertNotIn("abcdef-secret-token", record.getMessage())
        self.assertIn("***", record.getMessage())

    def test_filter_redacts_x_api_key_header(self) -> None:
        f = main_mod._SecretsRedactFilter()
        record = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="x-api-key: xkxk-shhh",
            args=(), exc_info=None,
        )
        f.filter(record)
        self.assertNotIn("xkxk-shhh", record.getMessage())


if __name__ == "__main__":
    unittest.main()
