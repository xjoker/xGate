"""单元测试：HMAC 签名 URL 工具。"""

from __future__ import annotations

import time
from urllib.parse import parse_qs

import pytest

from mini_grok_api.signed_url import sign_path, sign_file_url, verify_signed_path


_KEY = "test-api-key-12345"
_PATH = "/v1/files/image/abc123/foo.jpg"


# ---------------------------------------------------------------------------
# sign_path 基本格式
# ---------------------------------------------------------------------------

class TestSignPath:
    def test_returns_sig_and_exp(self):
        qs = sign_path(_PATH, _KEY)
        params = dict(p.split("=", 1) for p in qs.split("&"))
        assert "sig" in params
        assert "exp" in params

    def test_exp_is_in_future(self):
        qs = sign_path(_PATH, _KEY)
        params = dict(p.split("=", 1) for p in qs.split("&"))
        assert int(params["exp"]) > int(time.time())

    def test_custom_ttl(self):
        qs = sign_path(_PATH, _KEY, ttl_seconds=7200)
        params = dict(p.split("=", 1) for p in qs.split("&"))
        exp = int(params["exp"])
        # 应在 now+7200 附近（±5s 容忍）
        assert abs(exp - (int(time.time()) + 7200)) < 5

    def test_sig_is_hex(self):
        qs = sign_path(_PATH, _KEY)
        params = dict(p.split("=", 1) for p in qs.split("&"))
        sig = params["sig"]
        assert all(c in "0123456789abcdef" for c in sig)
        assert len(sig) == 64  # sha256 hex digest = 64 chars


# ---------------------------------------------------------------------------
# sign_file_url
# ---------------------------------------------------------------------------

class TestSignFileUrl:
    def test_includes_path_and_query(self):
        result = sign_file_url(_PATH, _KEY)
        assert result.startswith(_PATH + "?")
        assert "sig=" in result
        assert "exp=" in result


# ---------------------------------------------------------------------------
# round-trip：sign + verify
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_valid_signature_passes(self):
        qs = sign_path(_PATH, _KEY)
        params = dict(p.split("=", 1) for p in qs.split("&"))
        assert verify_signed_path(_PATH, params["sig"], int(params["exp"]), _KEY) is True

    def test_different_key_fails(self):
        qs = sign_path(_PATH, _KEY)
        params = dict(p.split("=", 1) for p in qs.split("&"))
        assert verify_signed_path(_PATH, params["sig"], int(params["exp"]), "wrong-key") is False

    def test_path_tamper_fails(self):
        qs = sign_path(_PATH, _KEY)
        params = dict(p.split("=", 1) for p in qs.split("&"))
        tampered = "/v1/files/image/abc123/evil.jpg"
        assert verify_signed_path(tampered, params["sig"], int(params["exp"]), _KEY) is False

    def test_sig_tamper_fails(self):
        qs = sign_path(_PATH, _KEY)
        params = dict(p.split("=", 1) for p in qs.split("&"))
        bad_sig = "a" * 64
        assert verify_signed_path(_PATH, bad_sig, int(params["exp"]), _KEY) is False


# ---------------------------------------------------------------------------
# 过期 exp
# ---------------------------------------------------------------------------

class TestExpiry:
    def test_expired_exp_fails(self):
        # 使用已过期的 exp（1 秒前）
        exp_past = int(time.time()) - 1
        import hashlib, hmac
        payload = f"{_PATH}|{exp_past}"
        sig = hmac.new(_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        assert verify_signed_path(_PATH, sig, exp_past, _KEY) is False

    def test_future_exp_passes(self):
        qs = sign_path(_PATH, _KEY, ttl_seconds=3600)
        params = dict(p.split("=", 1) for p in qs.split("&"))
        assert verify_signed_path(_PATH, params["sig"], int(params["exp"]), _KEY) is True


# ---------------------------------------------------------------------------
# 边界情况
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_sig_fails(self):
        qs = sign_path(_PATH, _KEY)
        params = dict(p.split("=", 1) for p in qs.split("&"))
        # 空签名应返回 False，不抛异常
        assert verify_signed_path(_PATH, "", int(params["exp"]), _KEY) is False

    def test_non_hex_sig_fails(self):
        qs = sign_path(_PATH, _KEY)
        params = dict(p.split("=", 1) for p in qs.split("&"))
        assert verify_signed_path(_PATH, "not-a-hex!", int(params["exp"]), _KEY) is False

    def test_video_path_round_trip(self):
        path = "/v1/files/video/sess-xyz/clip.mp4"
        qs = sign_path(path, _KEY)
        params = dict(p.split("=", 1) for p in qs.split("&"))
        assert verify_signed_path(path, params["sig"], int(params["exp"]), _KEY) is True

    def test_grok_files_path_round_trip(self):
        path = "/v1/grok-files/some-uuid-file.png"
        qs = sign_path(path, _KEY)
        params = dict(p.split("=", 1) for p in qs.split("&"))
        assert verify_signed_path(path, params["sig"], int(params["exp"]), _KEY) is True
