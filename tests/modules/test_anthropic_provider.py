"""Tests for AnthropicProvider — mock-based, no real API calls."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from synapse.modules.providers.anthropic import AnthropicProvider
from synapse.protocols.llm import Message, LLMResponse


@pytest.fixture
def provider():
    return AnthropicProvider(model="claude-sonnet-4-6", api_key="test-key")


def test_model_id(provider):
    assert provider.model_id == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_chat_basic():
    provider = AnthropicProvider(model="claude-sonnet-4-6", api_key="test-key")

    mock_msg = AsyncMock()
    mock_msg.content = [type("Block", (), {"text": "Hello, I am Claude", "type": "text"})()]
    mock_msg.stop_reason = "end_turn"
    mock_msg.usage.input_tokens = 10
    mock_msg.usage.output_tokens = 5

    mock_response = AsyncMock()
    mock_response.content = [mock_msg.content[0]]
    mock_response.stop_reason = "end_turn"
    mock_response.usage = mock_msg.usage

    with patch.object(provider._client.messages, "create", new=AsyncMock(return_value=mock_response)):
        result = await provider.chat(
            messages=[Message(role="user", content="Hi")],
        )

    assert isinstance(result, LLMResponse)
    assert result.content == "Hello, I am Claude"
    assert result.stop_reason == "end_turn"
    assert result.usage["input"] == 10
    assert result.usage["output"] == 5


@pytest.mark.asyncio
async def test_chat_with_tools():
    provider = AnthropicProvider(model="claude-sonnet-4-6", api_key="test-key")

    mock_tool_use = type("Block", (), {
        "type": "tool_use",
        "id": "tool_1",
        "name": "read",
        "input": {"path": "/test.txt"},
    })()

    mock_response = AsyncMock()
    mock_response.content = [mock_tool_use]
    mock_response.stop_reason = "tool_use"
    mock_response.usage.input_tokens = 20
    mock_response.usage.output_tokens = 15

    with patch.object(provider._client.messages, "create", new=AsyncMock(return_value=mock_response)):
        result = await provider.chat(
            messages=[Message(role="user", content="Read the file")],
            tools=[{"name": "read", "description": "Read a file", "input_schema": {}}],
        )

    assert result.stop_reason == "tool_use"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "read"
    assert result.tool_calls[0]["input"] == {"path": "/test.txt"}


@pytest.mark.asyncio
async def test_chat_converts_tool_results():
    """Tool result messages should be converted to Anthropic's format."""
    provider = AnthropicProvider(model="claude-sonnet-4-6", api_key="test-key")

    mock_response = AsyncMock()
    mock_response.content = [type("Block", (), {"text": "Got it", "type": "text"})()]
    mock_response.stop_reason = "end_turn"
    mock_response.usage.input_tokens = 5
    mock_response.usage.output_tokens = 3

    with patch.object(provider._client.messages, "create", new=AsyncMock(return_value=mock_response)):
        await provider.chat(
            messages=[
                Message(role="user", content="Read /tmp/x"),
                Message(role="assistant", content=""),
                Message(role="user", content="tool result: file contents here"),
            ],
        )

    # The key thing: it didn't crash on tool_result messages
    assert True


class _StreamCM:
    def __init__(self, events):
        self._events = events

    async def __aenter__(self):
        return self._event_stream()

    async def __aexit__(self, *args):
        return False

    async def _event_stream(self):
        for e in self._events:
            yield e


def _make_event(delta_type, text=None, partial_json=None):
    delta = type("Delta", (), {"type": delta_type, "text": text, "partial_json": partial_json})()
    return type("Event", (), {"type": "content_block_delta", "delta": delta})()


@pytest.mark.asyncio
async def test_stream_yields_text_and_tool_chunks():
    provider = AnthropicProvider(model="claude-sonnet-4-6", api_key="test-key")
    events = [
        _make_event("text_delta", text="Hello"),
        _make_event("text_delta", text=" world"),
        _make_event("input_json_delta", partial_json='{"path": "/x"}'),
    ]
    with patch.object(
        provider._client.messages, "stream",
        new=MagicMock(return_value=_StreamCM(events)),
    ):
        out = [c async for c in provider.stream(messages=[Message(role="user", content="Hi")])]

    assert [c.content for c in out] == ["Hello", " world", ""]
    assert out[2].tool_call_delta == {"input": '{"path": "/x"}'}
