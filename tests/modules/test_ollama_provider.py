"""Tests for OllamaProvider — mock-based, no real API calls."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from synapse.modules.providers.ollama import OllamaProvider
from synapse.protocols.llm import Message, LLMResponse

# Default Ollama base URL
OLLAMA_BASE = "http://localhost:11434/v1"


@pytest.fixture
def provider():
    return OllamaProvider(model="llama3.2")


def test_model_id(provider):
    assert provider.model_id == "llama3.2"


@pytest.mark.asyncio
async def test_chat_basic():
    provider = OllamaProvider(model="llama3.2")

    # Build a mock chat completion response
    mock_message = AsyncMock()
    mock_message.content = "Hello! How can I help you?"
    mock_message.tool_calls = None

    mock_choice = AsyncMock()
    mock_choice.message = mock_message
    mock_choice.finish_reason = "stop"

    mock_usage = AsyncMock()
    mock_usage.prompt_tokens = 10
    mock_usage.completion_tokens = 5

    mock_response = AsyncMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = mock_usage

    with patch.object(
        provider._client.chat.completions, "create", new=AsyncMock(return_value=mock_response)
    ):
        result = await provider.chat(
            messages=[Message(role="user", content="Hi")],
        )

    assert isinstance(result, LLMResponse)
    assert result.content == "Hello! How can I help you?"
    assert result.stop_reason == "stop"
    assert result.usage["input"] == 10
    assert result.usage["output"] == 5


def _make_chunk(content=None, tool_calls=None):
    delta = type("Delta", (), {"content": content, "tool_calls": tool_calls})()
    choice = type("Choice", (), {"delta": delta})()
    return type("Chunk", (), {"choices": [choice]})()


def _make_tool_call(index=0, id="call_1", name="read", arguments='{"path": "/x"}'):
    function = type("Function", (), {"name": name, "arguments": arguments})()
    return type("ToolCall", (), {"index": index, "id": id, "function": function})()


async def _chunk_stream(chunks):
    for c in chunks:
        yield c


@pytest.mark.asyncio
async def test_stream_yields_text_and_tool_chunks():
    provider = OllamaProvider(model="llama3.2")
    chunks = [
        _make_chunk(content="Hello"),
        _make_chunk(content=" world"),
        _make_chunk(content="", tool_calls=[_make_tool_call()]),
    ]
    with patch.object(
        provider._client.chat.completions, "create",
        new=MagicMock(return_value=_chunk_stream(chunks)),
    ):
        out = [c async for c in provider.stream(messages=[Message(role="user", content="Hi")])]

    assert [c.content for c in out] == ["Hello", " world", ""]
    assert out[2].tool_call_delta is not None
    assert out[2].tool_call_delta["name"] == "read"
