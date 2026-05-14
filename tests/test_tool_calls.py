"""基础版 tool_call 单元测试（single-shot prompt injection + output parsing）。

覆盖范围：
1. tools 传入 → _extract_prompt 包含工具描述
2. tool_choice="none" → prompt 不包含工具描述
3. 模型回 plain text → 走正常 content 分支（parse_tool_call 返回 None）
4. 模型回 {"tool_call": ...} → parse_tool_call 正确解析
5. chat_response_tool_call → 响应符合 OpenAI tool_calls 格式

不测试的部分（follow-up 任务）：
- 多轮 tool_call（tool 结果回填）
- 流式 tool_call 增量
- tool_choice 指定 function name 的强约束
"""

from __future__ import annotations

import json
import unittest

from mini_grok_api.openai_compat import chat_response_tool_call, parse_tool_call
from mini_grok_api.schemas import ChatCompletionRequest


# ---------------------------------------------------------------------------
# 辅助：从 ChatCompletionRequest 构造 req，调用 _extract_prompt
# ---------------------------------------------------------------------------

def _make_req(messages: list[dict], **kwargs) -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate({
        "model": "grok-4.20-auto",
        "messages": messages,
        **kwargs,
    })


def _extract(req: ChatCompletionRequest) -> str:
    from mini_grok_api.main import _extract_prompt
    return _extract_prompt(req)


# ---------------------------------------------------------------------------
# 测试 1：tools 传入时 prompt 包含工具描述
# ---------------------------------------------------------------------------

class ToolsInjectionTests(unittest.TestCase):
    _TOOLS = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get current weather for a city",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "City name"},
                    },
                    "required": ["city"],
                },
            },
        }
    ]

    def test_tools_injected_into_prompt(self) -> None:
        """tools 传入时，prompt 必须包含工具名和描述。"""
        req = _make_req(
            [{"role": "user", "content": "What is the weather?"}],
            tools=self._TOOLS,
        )
        prompt = _extract(req)
        self.assertIn("get_weather", prompt)
        self.assertIn("Get current weather for a city", prompt)
        self.assertIn("tool_call", prompt)

    def test_tool_choice_none_suppresses_injection(self) -> None:
        """tool_choice=none 时不注入工具描述。"""
        req = _make_req(
            [{"role": "user", "content": "Hello"}],
            tools=self._TOOLS,
            tool_choice="none",
        )
        prompt = _extract(req)
        self.assertNotIn("get_weather", prompt)
        self.assertNotIn("TOOL:", prompt)

    def test_system_message_preserved_with_tools(self) -> None:
        """有 system message 时，工具块拼在 system 之后，用户 system 内容仍保留。"""
        req = _make_req(
            [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "What is the weather?"},
            ],
            tools=self._TOOLS,
        )
        prompt = _extract(req)
        self.assertIn("You are a helpful assistant.", prompt)
        self.assertIn("get_weather", prompt)

    def test_tool_choice_required_appends_must_call(self) -> None:
        """tool_choice=required 时在工具块末尾追加强制提示。"""
        req = _make_req(
            [{"role": "user", "content": "Do something"}],
            tools=self._TOOLS,
            tool_choice="required",
        )
        prompt = _extract(req)
        self.assertIn("MUST call a tool", prompt)

    def test_no_tools_no_injection(self) -> None:
        """未传 tools 时 prompt 不包含 TOOL: 前缀。"""
        req = _make_req(
            [{"role": "user", "content": "Just chat"}],
        )
        prompt = _extract(req)
        self.assertNotIn("TOOL:", prompt)
        self.assertNotIn("tool_call", prompt)


# ---------------------------------------------------------------------------
# 测试 2：parse_tool_call 解析逻辑
# ---------------------------------------------------------------------------

class ParseToolCallTests(unittest.TestCase):
    def test_plain_text_returns_none(self) -> None:
        """模型回 plain text 时 parse_tool_call 返回 None。"""
        result = parse_tool_call("The weather is sunny today.")
        self.assertIsNone(result)

    def test_valid_tool_call_json_parsed(self) -> None:
        """模型回标准格式时正确解析 name 和 arguments。"""
        model_output = '{"tool_call": {"name": "get_weather", "arguments": {"city": "Beijing"}}}'
        result = parse_tool_call(model_output)
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "get_weather")
        self.assertEqual(result["arguments"], {"city": "Beijing"})

    def test_tool_call_with_whitespace(self) -> None:
        """允许 JSON 前后有空白字符。"""
        model_output = '  \n{"tool_call": {"name": "search", "arguments": {"q": "test"}}}\n  '
        result = parse_tool_call(model_output)
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "search")

    def test_invalid_json_returns_none(self) -> None:
        """格式破损的 JSON 返回 None，不报错。"""
        result = parse_tool_call('{"tool_call": {broken json}')
        self.assertIsNone(result)

    def test_missing_name_field_returns_none(self) -> None:
        """tool_call 缺少 name 字段时返回 None。"""
        result = parse_tool_call('{"tool_call": {"arguments": {"x": 1}}}')
        self.assertIsNone(result)

    def test_partial_match_returns_none(self) -> None:
        """输出只是 tool_call JSON 的一部分（如前面有文字）时返回 None。"""
        result = parse_tool_call('Here is the call: {"tool_call": {"name": "f", "arguments": {}}}')
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# 测试 3：chat_response_tool_call 响应格式
# ---------------------------------------------------------------------------

class ChatResponseToolCallTests(unittest.TestCase):
    def test_response_shape(self) -> None:
        """验证 tool_call 响应符合 OpenAI tool_calls 格式。"""
        resp = chat_response_tool_call(
            "grok-4.20-auto",
            "get_weather",
            {"city": "Shanghai"},
            "What is the weather?",
        )
        self.assertEqual(resp["object"], "chat.completion")
        choice = resp["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        msg = choice["message"]
        self.assertEqual(msg["role"], "assistant")
        self.assertIsNone(msg["content"])
        self.assertIsInstance(msg["tool_calls"], list)
        self.assertEqual(len(msg["tool_calls"]), 1)
        tc = msg["tool_calls"][0]
        self.assertEqual(tc["type"], "function")
        self.assertTrue(tc["id"].startswith("call_"))
        fn = tc["function"]
        self.assertEqual(fn["name"], "get_weather")
        # arguments 必须是 JSON string
        args = json.loads(fn["arguments"])
        self.assertEqual(args["city"], "Shanghai")

    def test_arguments_already_string_passthrough(self) -> None:
        """arguments 已经是 string 时直接透传，不二次序列化。"""
        args_str = '{"city": "Guangzhou"}'
        resp = chat_response_tool_call("grok-4.20-auto", "get_weather", args_str, "prompt")
        fn = resp["choices"][0]["message"]["tool_calls"][0]["function"]
        self.assertEqual(fn["arguments"], args_str)

    def test_usage_fields_present(self) -> None:
        """响应必须包含 usage 字段。"""
        resp = chat_response_tool_call("grok-4.20-auto", "f", {}, "p")
        self.assertIn("usage", resp)
        self.assertIn("total_tokens", resp["usage"])


# ---------------------------------------------------------------------------
# 测试 4：多轮 tool_call 支持（multi-turn）
# ---------------------------------------------------------------------------

class MultiTurnToolCallTests(unittest.TestCase):
    _TOOLS = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather for a city",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
            },
        }
    ]

    def test_tool_role_message_serialized_with_name(self) -> None:
        """role=tool 消息能正确序列化，并反查到 tool name（而非 tool_call_id）。"""
        req = _make_req(
            [
                {"role": "user", "content": "What's the weather in Beijing?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"Beijing"}'}}
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": '{"temp": 15, "condition": "cloudy"}'},
            ],
            tools=self._TOOLS,
        )
        prompt = _extract(req)
        # tool result 应该显示工具名，而不仅是 call_1
        self.assertIn("get_weather", prompt)
        self.assertIn("returned", prompt)
        self.assertIn('{"temp": 15', prompt)

    def test_tool_role_fallback_to_call_id_when_no_name(self) -> None:
        """无法反查 name 时，tool result 回退使用 tool_call_id 标注。"""
        req = _make_req(
            [
                {"role": "user", "content": "Do something"},
                # 故意不包含 assistant 消息，使反查失败
                {"role": "tool", "tool_call_id": "call_orphan", "content": "some result"},
            ],
        )
        prompt = _extract(req)
        self.assertIn("call_orphan", prompt)
        self.assertIn("some result", prompt)

    def test_assistant_tool_calls_serialized(self) -> None:
        """role=assistant 带 tool_calls 且 content=None 时正确序列化为 [assistant called tool ...] 格式。"""
        req = _make_req(
            [
                {"role": "user", "content": "Check weather"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "call_2", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"Shanghai"}'}}
                    ],
                },
            ],
            tools=self._TOOLS,
        )
        prompt = _extract(req)
        self.assertIn("assistant called tool", prompt)
        self.assertIn("get_weather", prompt)
        self.assertIn("Shanghai", prompt)

    def test_full_multi_turn_prompt_contains_all_parts(self) -> None:
        """完整多轮场景：user → assistant(tool_call) → tool → user，prompt 包含所有信息。"""
        req = _make_req(
            [
                {"role": "user", "content": "What's the weather in Beijing?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "call_3", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"Beijing"}'}}
                    ],
                },
                {"role": "tool", "tool_call_id": "call_3", "content": '{"temp": 15, "condition": "cloudy"}'},
                {"role": "user", "content": "Is that cold?"},
            ],
            tools=self._TOOLS,
        )
        prompt = _extract(req)
        # 用户问题
        self.assertIn("What's the weather in Beijing?", prompt)
        # assistant 调工具
        self.assertIn("assistant called tool", prompt)
        self.assertIn("get_weather", prompt)
        # tool 返回结果（应显示 tool name 而非 id）
        self.assertIn("tool `get_weather` returned", prompt)
        self.assertIn('{"temp": 15', prompt)
        # 后续用户消息
        self.assertIn("Is that cold?", prompt)

    def test_tools_system_block_contains_multi_turn_hint(self) -> None:
        """_build_tools_system_block 末尾包含多轮提示语句。"""
        req = _make_req(
            [{"role": "user", "content": "Hello"}],
            tools=self._TOOLS,
        )
        prompt = _extract(req)
        self.assertIn("Previous tool calls and their results are shown", prompt)
        self.assertIn("respond with plain text", prompt)


if __name__ == "__main__":
    unittest.main()
