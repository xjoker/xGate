"""PR 1：parse_event replay 测试。

使用 tests/fixtures/grok_sse/probe-auto-9df3.jsonl 离线 fixture，
无网络调用。

fixture 含第三方用户数据（真实 X 用户名/头像），仅本地存储，不提交至仓库。
fixture 不存在时，依赖它的测试会自动 skip；parse_event / classify_line
的纯逻辑测试（test 10/11）不受影响，始终运行。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_grok_api.grok_client import (
    GrokCitation,
    GrokConversationStarted,
    GrokDone,
    GrokFinalToken,
    GrokImageEvent,
    GrokReasoningHeader,
    GrokReasoningToken,
    GrokToolCall,
    GrokWebSearchResults,
    GrokXSearchResults,
    classify_line,
    parse_event,
)

FIXTURE = Path(__file__).parent / "fixtures" / "grok_sse" / "probe-auto-9df3.jsonl"


def _load_events() -> list:
    """从 fixture 文件解析出所有 GrokEvent，过滤空列表。"""
    events = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        for ev in parse_event(line):
            events.append(ev)
    return events


@pytest.fixture(scope="module")
def all_events():
    if not FIXTURE.exists():
        pytest.skip(
            "local SSE fixture not available "
            "(contains third-party user data, stored locally only)"
        )
    return _load_events()


# ──────────────────────────────────────────────────────────────────────────────
# 1. conversationId 抽取
# ──────────────────────────────────────────────────────────────────────────────

def test_conversation_started(all_events):
    started = [e for e in all_events if isinstance(e, GrokConversationStarted)]
    assert len(started) == 1
    assert started[0].conversation_id == "2f0e4e1c-b42e-4f73-9e00-a8225e6812d7"


# ──────────────────────────────────────────────────────────────────────────────
# 2. Reasoning token：header vs summary 区分
# ──────────────────────────────────────────────────────────────────────────────

def test_reasoning_header_emitted(all_events):
    headers = [e for e in all_events if isinstance(e, GrokReasoningHeader)]
    assert len(headers) >= 1
    first = headers[0]
    assert first.label == "Searching recent posts"
    assert first.rollout == "Grok"
    assert first.step_id == 1


def test_reasoning_tokens_emitted(all_events):
    tokens = [e for e in all_events if isinstance(e, GrokReasoningToken)]
    assert len(tokens) >= 1
    assert all(isinstance(t.token, str) for t in tokens)
    assert all(isinstance(t.rollout, str) for t in tokens)


# ──────────────────────────────────────────────────────────────────────────────
# 3. xSearchResults 解析完整
# ──────────────────────────────────────────────────────────────────────────────

def test_x_search_results_count(all_events):
    xsr_events = [e for e in all_events if isinstance(e, GrokXSearchResults)]
    assert len(xsr_events) >= 1


def test_x_search_results_fields(all_events):
    xsr_events = [e for e in all_events if isinstance(e, GrokXSearchResults)]
    # 取第一批结果中的第一条验证字段完整性
    first_result = xsr_events[0].results[0]
    required_fields = {"username", "name", "text", "postId", "createTime"}
    for field in required_fields:
        assert field in first_result, f"missing field: {field}"
    assert isinstance(first_result.get("viewCount"), int)


# ──────────────────────────────────────────────────────────────────────────────
# 4. webSearchResults 解析完整
# ──────────────────────────────────────────────────────────────────────────────

def test_web_search_results(all_events):
    wsr_events = [e for e in all_events if isinstance(e, GrokWebSearchResults)]
    assert len(wsr_events) >= 1
    first = wsr_events[0].results[0]
    assert "url" in first
    assert "title" in first
    assert "preview" in first


# ──────────────────────────────────────────────────────────────────────────────
# 5. toolUsageCard：xSearch 和 webSearch 两种类型识别
# ──────────────────────────────────────────────────────────────────────────────

def test_tool_calls_identified(all_events):
    tool_calls = [e for e in all_events if isinstance(e, GrokToolCall)]
    assert len(tool_calls) >= 2
    tools_seen = {tc.tool for tc in tool_calls}
    assert "xSearch" in tools_seen
    assert "webSearch" in tools_seen


def test_tool_call_has_args(all_events):
    tool_calls = [e for e in all_events if isinstance(e, GrokToolCall)]
    for tc in tool_calls:
        assert isinstance(tc.args, dict)
        assert isinstance(tc.card_id, str) and tc.card_id
        # xSearch / webSearch 带 query 字段；chatroomSend 带 message 字段
        if tc.tool in ("xSearch", "webSearch"):
            assert "query" in tc.args
        elif tc.tool == "chatroomSend":
            assert "message" in tc.args


# ──────────────────────────────────────────────────────────────────────────────
# 6. cardAttachment：citation_card 识别
# ──────────────────────────────────────────────────────────────────────────────

def test_citations_parsed(all_events):
    citations = [e for e in all_events if isinstance(e, GrokCitation)]
    assert len(citations) >= 1
    for c in citations:
        assert c.url.startswith("http")
        assert isinstance(c.card_id, str)


def test_no_spurious_image_events(all_events):
    # fixture 是文字搜索，不应产出真实图片 URL（只可能有 placeholder）
    img_events = [e for e in all_events if isinstance(e, GrokImageEvent) and e.image_urls]
    assert len(img_events) == 0


# ──────────────────────────────────────────────────────────────────────────────
# 7. finalMetadata.followUpSuggestions 抽取
# ──────────────────────────────────────────────────────────────────────────────

def test_final_metadata_follow_ups(all_events):
    done_events = [e for e in all_events if isinstance(e, GrokDone)]
    # 至少有一个 GrokDone 含 followUpSuggestions
    done_with_followups = [e for e in done_events if e.follow_up_suggestions]
    assert len(done_with_followups) >= 1
    followups = done_with_followups[0].follow_up_suggestions
    assert len(followups) >= 2
    assert all(isinstance(f, str) and f for f in followups)


# ──────────────────────────────────────────────────────────────────────────────
# 8. responseId 抽取
# ──────────────────────────────────────────────────────────────────────────────

def test_response_id_in_done(all_events):
    done_events = [e for e in all_events if isinstance(e, GrokDone)]
    assert len(done_events) >= 1
    # 所有 GrokDone 都应有非空 response_id
    for d in done_events:
        assert d.response_id, f"GrokDone missing response_id: {d}"


# ──────────────────────────────────────────────────────────────────────────────
# 9. Final token 流（正文 token）
# ──────────────────────────────────────────────────────────────────────────────

def test_final_tokens_emitted(all_events):
    final_tokens = [e for e in all_events if isinstance(e, GrokFinalToken)]
    assert len(final_tokens) >= 100  # fixture 有 526 条 final token
    full_text = "".join(t.token for t in final_tokens)
    assert len(full_text) > 200


# ──────────────────────────────────────────────────────────────────────────────
# 10. parse_event 容错：非法 JSON 不崩溃
# ──────────────────────────────────────────────────────────────────────────────

def test_parse_event_invalid_json():
    assert parse_event("not json") == []
    assert parse_event("") == []
    assert parse_event('{"result": null}') == []


# ──────────────────────────────────────────────────────────────────────────────
# 11. classify_line 与 parse_event 协作（DONE 不会进入解析）
# ──────────────────────────────────────────────────────────────────────────────

def test_classify_done_line():
    ev, data = classify_line("data: [DONE]")
    assert ev == "done"
    assert data == ""


def test_classify_data_line():
    ev, data = classify_line('data: {"result":{}}')
    assert ev == "data"
    assert data == '{"result":{}}'
