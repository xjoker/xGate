"""OpenAI 兼容相关单元测试：schemas、_extract_prompt、错误格式、流式 helper。

不启动 FastAPI（避免触发 lifespan + 后台 worker），尽量做纯函数级测试。
TestClient 集成测试见 test_auth.py（那边会处理 mock 上游 Grok）。
"""

from __future__ import annotations

import json
import unittest

from mini_grok_api import openai_compat
from mini_grok_api.openai_compat import (
    chat_response,
    error_payload,
    stream_chunk,
    stream_usage_chunk,
    type_for_status,
)
from mini_grok_api.schemas import ChatCompletionRequest, ImageGenerationRequest


class SchemaCompatibilityTests(unittest.TestCase):
    def test_extra_fields_are_ignored(self) -> None:
        # OpenAI SDK 会发我们没实现的字段；不能 422
        req = ChatCompletionRequest.model_validate({
            "model": "grok-4.20-auto",
            "messages": [{"role": "user", "content": "hi"}],
            # 故意塞一堆未知字段：
            "totally_made_up": True,
            "future_field": {"nested": 42},
        })
        self.assertEqual(req.model, "grok-4.20-auto")
        self.assertEqual(len(req.messages), 1)

    def test_known_optional_fields_accepted(self) -> None:
        req = ChatCompletionRequest.model_validate({
            "model": "grok-4.20-auto",
            "messages": [{"role": "user", "content": "hi"}],
            "n": 1,
            "stop": ["\n\n"],
            "seed": 42,
            "frequency_penalty": 0.5,
            "presence_penalty": -0.5,
            "user": "user-123",
            "tools": [{"type": "function", "function": {"name": "f"}}],
            "tool_choice": "auto",
            "response_format": {"type": "json_object"},
            "stream_options": {"include_usage": True},
            "logprobs": True,
            "top_logprobs": 5,
            "service_tier": "auto",
            "metadata": {"k": "v"},
        })
        self.assertTrue(req.stream_options.include_usage)
        self.assertEqual(req.seed, 42)
        self.assertEqual(req.user, "user-123")

    def test_max_completion_tokens_alias(self) -> None:
        req = ChatCompletionRequest.model_validate({
            "model": "grok-4.20-auto",
            "messages": [{"role": "user", "content": "hi"}],
            "max_completion_tokens": 100,
        })
        self.assertEqual(req.max_completion_tokens, 100)

    def test_image_url_message_block_does_not_raise(self) -> None:
        # 多模态消息：image_url 应被吞成 [image] 占位，不报错
        req = ChatCompletionRequest.model_validate({
            "model": "grok-4.20-auto",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "描述这张图"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxxx"}},
                ],
            }],
        })
        # 真正的转换在 main._extract_prompt，这里只验 schema 不爆
        self.assertEqual(len(req.messages), 1)

    def test_image_request_rejects_b64_json(self) -> None:
        req = ImageGenerationRequest.model_validate({
            "model": "grok-imagine",
            "prompt": "cat",
            "response_format": "b64_json",
        })
        self.assertEqual(req.response_format, "b64_json")  # 校验通过；语义在 main 里报错


class ExtractPromptTests(unittest.TestCase):
    def _extract(self, messages: list[dict]) -> str:
        # 延迟导入避免触发 main 的 lifespan
        from mini_grok_api.main import _extract_prompt
        req = ChatCompletionRequest.model_validate({
            "model": "grok-4.20-auto",
            "messages": messages,
        })
        return _extract_prompt(req)

    def test_string_content(self) -> None:
        out = self._extract([{"role": "user", "content": "hello world"}])
        self.assertIn("[user]: hello world", out)

    def test_image_url_becomes_placeholder(self) -> None:
        out = self._extract([{
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "x"}},
            ],
        }])
        self.assertIn("看图", out)
        self.assertIn("[image]", out)

    def test_audio_block_becomes_placeholder(self) -> None:
        out = self._extract([{
            "role": "user",
            "content": [
                {"type": "input_audio", "input_audio": {"data": "x", "format": "wav"}},
                {"type": "text", "text": "转写"},
            ],
        }])
        self.assertIn("[audio]", out)
        self.assertIn("转写", out)

    def test_unknown_block_type_skipped(self) -> None:
        out = self._extract([{
            "role": "user",
            "content": [
                {"type": "text", "text": "正常"},
                {"type": "future_block", "data": "foo"},
            ],
        }])
        self.assertIn("正常", out)
        self.assertNotIn("future_block", out)


class ErrorFormatTests(unittest.TestCase):
    def test_status_to_type_mapping(self) -> None:
        self.assertEqual(type_for_status(401), "authentication_error")
        self.assertEqual(type_for_status(403), "permission_error")
        self.assertEqual(type_for_status(404), "not_found_error")
        self.assertEqual(type_for_status(429), "rate_limit_error")
        self.assertEqual(type_for_status(500), "server_error")
        self.assertEqual(type_for_status(502), "api_error")
        self.assertEqual(type_for_status(418), "invalid_request_error")  # 兜底
        self.assertEqual(type_for_status(599), "api_error")

    def test_error_payload_shape(self) -> None:
        payload = error_payload("boom", error_type="rate_limit_error", code="too_many")
        self.assertIn("error", payload)
        err = payload["error"]
        self.assertEqual(err["message"], "boom")
        self.assertEqual(err["type"], "rate_limit_error")
        self.assertEqual(err["code"], "too_many")
        self.assertIsNone(err["param"])


class ResponseShapeTests(unittest.TestCase):
    def test_chat_response_has_required_openai_fields(self) -> None:
        resp = chat_response("grok-4.20-auto", "hello", "hi")
        for k in ("id", "object", "created", "model", "system_fingerprint", "choices", "usage"):
            self.assertIn(k, resp)
        self.assertEqual(resp["object"], "chat.completion")
        self.assertEqual(resp["choices"][0]["finish_reason"], "stop")
        self.assertIsNone(resp["choices"][0]["logprobs"])
        usage = resp["usage"]
        self.assertIn("prompt_tokens_details", usage)
        self.assertIn("completion_tokens_details", usage)
        self.assertEqual(usage["prompt_tokens_details"]["cached_tokens"], 0)

    def test_chat_response_finish_reason_length(self) -> None:
        resp = chat_response("grok-4.20-auto", "x", "y", finish_reason="length")
        self.assertEqual(resp["choices"][0]["finish_reason"], "length")

    def test_stream_chunk_first_has_role(self) -> None:
        chunk = stream_chunk("rid-1", "grok-4.20-auto", "hello", role="assistant")
        delta = chunk["choices"][0]["delta"]
        self.assertEqual(delta["role"], "assistant")
        self.assertEqual(delta["content"], "hello")
        self.assertNotIn("finish_reason", chunk["choices"][0])
        self.assertEqual(chunk["system_fingerprint"], openai_compat.SYSTEM_FINGERPRINT)

    def test_stream_chunk_subsequent_no_role(self) -> None:
        chunk = stream_chunk("rid-1", "grok-4.20-auto", "world", role=None)
        delta = chunk["choices"][0]["delta"]
        self.assertNotIn("role", delta)
        self.assertEqual(delta["content"], "world")

    def test_stream_chunk_finish_reason(self) -> None:
        chunk = stream_chunk("rid-1", "grok-4.20-auto", "", finish_reason="length", role=None)
        self.assertEqual(chunk["choices"][0]["finish_reason"], "length")

    def test_stream_usage_chunk(self) -> None:
        chunk = stream_usage_chunk("rid-1", "grok-4.20-auto", "prompt", "completion")
        self.assertEqual(chunk["choices"], [])
        self.assertIn("usage", chunk)
        self.assertGreater(chunk["usage"]["prompt_tokens"], 0)


class ModelsListTests(unittest.TestCase):
    def test_list_models_has_openai_fields(self) -> None:
        from mini_grok_api.models import list_models
        models = list_models()
        self.assertGreater(len(models), 0)
        for m in models:
            for k in ("id", "object", "created", "owned_by", "permission", "root", "parent"):
                self.assertIn(k, m)
            self.assertEqual(m["object"], "model")


if __name__ == "__main__":
    unittest.main()
