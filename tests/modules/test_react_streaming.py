"""Tests for ReActPlanner._call_llm_stream — mock-based, no real API calls."""
import pytest
from synapse.modules.planning.react import ReActPlanner
from synapse.protocols.llm import LLMChunk, Message


class _FakeEventBus:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


class _FakeLLM:
    def __init__(self, chunks):
        self._chunks = chunks

    async def stream(self, messages, tools=None):
        for c in self._chunks:
            yield c


def _text_and_tool_chunks():
    return [
        LLMChunk(content="Let me "),
        LLMChunk(content="search."),
        LLMChunk(tool_call_delta={"index": 0, "id": "call_1", "name": "grep",
                                  "input": '{"pattern": "foo"}'}),
        LLMChunk(usage={"input": 10, "output": 5}),
    ]


def _google_style_chunks():
    # Gemini streams the tool input as a ready-made dict, not a JSON string.
    return [
        LLMChunk(content="ok"),
        LLMChunk(tool_call_delta={"index": 0, "id": "c1", "name": "read",
                                  "input": {"path": "/x"}}),
    ]


@pytest.mark.asyncio
async def test_call_llm_stream_reconstructs_tool_calls_and_emits_tokens():
    llm = _FakeLLM(_text_and_tool_chunks())
    bus = _FakeEventBus()
    resp = await ReActPlanner()._call_llm_stream(
        llm, [Message(role="user", content="x")], None, bus, "sess1")

    assert resp.content == "Let me search."
    assert resp.tool_calls == [{"id": "call_1", "name": "grep", "input": {"pattern": "foo"}}]
    assert resp.usage == {"input": 10, "output": 5}
    assert resp.stop_reason == "tool_use"

    token_events = [e for e in bus.events if e.event_type == "llm_token"]
    assert "".join(e.text for e in token_events) == "Let me search."


@pytest.mark.asyncio
async def test_call_llm_stream_preserves_dict_tool_input():
    llm = _FakeLLM(_google_style_chunks())
    resp = await ReActPlanner()._call_llm_stream(
        llm, [Message(role="user", content="x")], None, _FakeEventBus(), "sess2")

    assert resp.tool_calls == [{"id": "c1", "name": "read", "input": {"path": "/x"}}]


@pytest.mark.asyncio
async def test_call_llm_stream_final_turn_has_no_tool_calls():
    llm = _FakeLLM([LLMChunk(content="Done."), LLMChunk(usage={"input": 3, "output": 1})])
    resp = await ReActPlanner()._call_llm_stream(
        llm, [Message(role="user", content="x")], None, _FakeEventBus(), "sess3")

    assert resp.content == "Done."
    assert resp.tool_calls == []
    assert resp.stop_reason == "end_turn"
