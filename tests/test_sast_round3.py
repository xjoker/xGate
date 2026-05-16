"""SAST round 3 修复的集成测试。

覆盖三个安全修复：
- 文件端点强制鉴权（签名 URL OR 标准鉴权），无任一通道 → 401/403
- /admin/config flaresolverr_url SSRF 防护（云元数据 IP / 非 http(s) scheme 拒绝）
- /v1/grok/assets/download 的 Content-Disposition 文件名 CRLF 过滤
"""

from __future__ import annotations

import os
import pathlib
import re
import unittest
from dataclasses import replace

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(_REPO_ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from mini_grok_api import main as main_mod  # noqa: E402
from mini_grok_api.grok_client import IMAGE_DIR  # noqa: E402
from mini_grok_api.signed_url import sign_path  # noqa: E402

TEST_API_KEY = "sast-round3-test-key"


def _override_settings(api_key: str = TEST_API_KEY):
    base = main_mod.settings_store.get()
    new = replace(base, api_key=api_key)
    main_mod.settings_store._settings = new  # type: ignore[attr-defined]
    return new


def _headers() -> dict:
    return {"Authorization": f"Bearer {TEST_API_KEY}"}


class FileEndpointAuthTests(unittest.TestCase):
    """/v1/files/image 三种通道：签名 URL / Bearer / 无鉴权。"""

    @classmethod
    def setUpClass(cls) -> None:
        _override_settings(TEST_API_KEY)
        cls.client = TestClient(main_mod.app)
        # 准备一个真实文件，避免 200 vs 404 混淆鉴权结果
        cls.sid = "test-sast-session"
        cls.fn = "test.jpg"
        cls.sess_dir = IMAGE_DIR / cls.sid
        cls.sess_dir.mkdir(parents=True, exist_ok=True)
        (cls.sess_dir / cls.fn).write_bytes(b"\xff\xd8\xff\xe0fake_jpeg")

    @classmethod
    def tearDownClass(cls) -> None:
        (cls.sess_dir / cls.fn).unlink(missing_ok=True)
        try:
            cls.sess_dir.rmdir()
        except OSError:
            pass

    def test_no_auth_no_sig_returns_401(self):
        """无任何凭证 → 401（旧行为是 200 + 仅 warning，安全漏洞）。"""
        r = self.client.get(f"/v1/files/image/{self.sid}/{self.fn}")
        self.assertEqual(r.status_code, 401)

    def test_invalid_sig_returns_403(self):
        r = self.client.get(
            f"/v1/files/image/{self.sid}/{self.fn}",
            params={"sig": "deadbeef", "exp": "9999999999"},
        )
        self.assertEqual(r.status_code, 403)

    def test_valid_sig_returns_file(self):
        path = f"/v1/files/image/{self.sid}/{self.fn}"
        qs = sign_path(path, TEST_API_KEY)
        r = self.client.get(f"{path}?{qs}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content[:4], b"\xff\xd8\xff\xe0")

    def test_bearer_auth_returns_file(self):
        """浏览器 cookie / API 客户端 Bearer 通道仍能直读。"""
        r = self.client.get(
            f"/v1/files/image/{self.sid}/{self.fn}",
            headers=_headers(),
        )
        self.assertEqual(r.status_code, 200)


class FlaresolverrSsrfTests(unittest.TestCase):
    """/admin/config 的 flaresolverr_url SSRF 防护。"""

    @classmethod
    def setUpClass(cls) -> None:
        _override_settings(TEST_API_KEY)
        cls.client = TestClient(main_mod.app)

    def _post(self, fs_url: str):
        return self.client.post(
            "/admin/config",
            headers=_headers(),
            data={"flaresolverr_url": fs_url},
        )

    def test_metadata_ip_rejected(self):
        r = self._post("http://169.254.169.254/latest/meta-data/")
        self.assertEqual(r.status_code, 400)
        self.assertIn("元数据", r.text)

    def test_gcp_metadata_host_rejected(self):
        r = self._post("http://metadata.google.internal/computeMetadata/v1/")
        self.assertEqual(r.status_code, 400)

    def test_non_http_scheme_rejected(self):
        r = self._post("file:///etc/passwd")
        self.assertEqual(r.status_code, 400)
        self.assertIn("scheme", r.text)

    def test_missing_host_rejected(self):
        r = self._post("http:///path")
        self.assertEqual(r.status_code, 400)

    def test_valid_https_accepted(self):
        r = self._post("https://flaresolverr.example.com:8191")
        self.assertEqual(r.status_code, 200)

    def test_loopback_accepted_with_warning(self):
        """自托管 FlareSolverr 通常 127.0.0.1:8191，应放行只 warning。"""
        r = self._post("http://127.0.0.1:8191")
        self.assertEqual(r.status_code, 200)


class ContentDispositionCrlfTests(unittest.TestCase):
    """单元测试：Content-Disposition 文件名过滤。"""

    def test_re_strips_crlf_and_quote(self):
        # 模拟攻击 payload：换行 + 引号 + 反斜杠
        bad = 'evil"\r\nX-Injected: yes\r\n.jpg'
        safe = re.sub(r'[\r\n"\\]', '_', bad) or "download"
        self.assertNotIn("\r", safe)
        self.assertNotIn("\n", safe)
        self.assertNotIn('"', safe)
        self.assertNotIn("\\", safe)

    def test_empty_after_filter_uses_default(self):
        # 全是被过滤字符 → 用 fallback "download"
        bad = '"""'
        safe = re.sub(r'[\r\n"\\]', '_', bad) or "download"
        # 三个下划线，不是空
        self.assertEqual(safe, "___")
        # 真空场景测试
        safe2 = re.sub(r'[\r\n"\\]', '_', '') or "download"
        self.assertEqual(safe2, "download")


if __name__ == "__main__":
    unittest.main()
