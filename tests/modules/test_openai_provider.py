"""Tests for OpenAIProvider — mock-based, no real API calls."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from synapse.modules.providers.openai import OpenAIProvider
from synapse.protocols.llm import Message, LLMResponse


@pytest.fixture
def provider():
    return OpenAIProvider(model="gpt-4o", api_key="test-key")


def test_model_id(provider):
    assert provider.model_id == "gpt-4o"


@pytest.mark.asyncio
async def test_chat_basic():
    provider = OpenAIProvider(model="gpt-4o", api_key="test-key")

    mock_choice = type("Choice", (), {
        "message": type("Msg", (), {
            "content": "Hello, I am GPT",
            "tool_calls": None,
        })(),
        "finish_reason": "stop",
    })()

    mock_usage = type("Usage", (), {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    })()

    mock_response = type("Response", (), {
        "choices": [mock_choice],
        "usage": mock_usage,
    })()

    with patch.object(provider._client.chat.completions, "create", new=AsyncMock(return_value=mock_response)):
        result = await provider.chat(
            messages=[Message(role="user", content="Hi")],
        )

    assert isinstance(result, LLMResponse)
    assert result.content == "Hello, I am GPT"
    assert result.stop_reason == "stop"
    assert result.usage["input"] == 10
    assert result.usage["output"] == 5


@pytest.mark.asyncio
async def test_chat_with_tools():
    provider = OpenAIProvider(model="gpt-4o", api_key="test-key")

    mock_function = type("Function", (), {
        "name": "read",
        "arguments": json.dumps({"path": "/test.txt"}),
    })()

    mock_tool_call = type("ToolCall", (), {
        "id": "call_abc123",
        "function": mock_function,
    })()

    mock_message = type("Msg", (), {
        "content": None,
        "tool_calls": [mock_tool_call],
    })()

    mock_choice = type("Choice", (), {
        "message": mock_message,
        "finish_reason": "tool_calls",
    })()

    mock_usage = type("Usage", (), {
        "prompt_tokens": 20,
        "completion_tokens": 15,
        "total_tokens": 35,
    })()

    mock_response = type("Response", (), {
        "choices": [mock_choice],
        "usage": mock_usage,
    })()

    with patch.object(provider._client.chat.completions, "create", new=AsyncMock(return_value=mock_response)):
        result = await provider.chat(
            messages=[Message(role="user", content="Read the file")],
            tools=[{"name": "read", "description": "Read a file", "parameters": {}}],
        )

    assert result.stop_reason == "tool_use"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "read"
    assert result.tool_calls[0]["input"] == {"path": "/test.txt"}


def _make_chunk(content=None, tool_calls=None, usage=None):
    delta = type("Delta", (), {"content": content, "tool_calls": tool_calls})()
    choice = type("Choice", (), {"delta": delta})()
    return type("Chunk", (), {"choices": [choice], "usage": usage})()


def _make_tool_call(index=0, id="call_1", name="read", arguments='{"path": "/x"}'):
    function = type("Function", (), {"name": name, "arguments": arguments})()
    return type("ToolCall", (), {"index": index, "id": id, "function": function})()


async def _chunk_stream(chunks):
    for c in chunks:
        yield c


@pytest.mark.asyncio
async def test_stream_yields_text_and_tool_chunks():
    provider = OpenAIProvider(model="gpt-4o", api_key="test-key")
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


@pytest.mark.asyncio
async def test_stream_emits_usage_per_chunk():
    """Authoritative server usage (per-chunk or final) must win over tiktoken.
    The first chunk has no server usage, so it gets a tiktoken live count; the
    remaining chunks carry server usage and are emitted verbatim. DeepSeek/Ollama
    inherit this method.
    """
    provider = OpenAIProvider(model="gpt-4o", api_key="test-key")

    def _usage(inp, out):
        return type("U", (), {"prompt_tokens": inp, "completion_tokens": out})()

    chunks = [
        _make_chunk(content="Hello"),
        _make_chunk(content=" world", usage=_usage(10, 1)),
        _make_chunk(content="!", usage=_usage(10, 2)),
        _make_chunk(usage=_usage(10, 3)),  # final, usage-only chunk
    ]
    with patch.object(
        provider._client.chat.completions, "create",
        new=MagicMock(return_value=_chunk_stream(chunks)),
    ):
        out = [c async for c in provider.stream(messages=[Message(role="user", content="Hi")])]

    usage_chunks = [c for c in out if c.usage]
    # First live count comes from tiktoken (no server usage yet).
    assert usage_chunks[0].usage["input"] == 0
    assert usage_chunks[0].usage["output"] > 0
    # Then the server's authoritative per-chunk usage, verbatim.
    assert [c.usage for c in usage_chunks[1:]] == [
        {"input": 10, "output": 1},
        {"input": 10, "output": 2},
        {"input": 10, "output": 3},
    ]
    # Text must still stream alongside usage.
    assert "".join(c.content or "" for c in out) == "Hello world!"


@pytest.mark.asyncio
async def test_stream_emits_tiktoken_usage_when_no_server_usage():
    """When the server reports no usage until the end (standard OpenAI/DeepSeek),
    streamed text is counted live with tiktoken so the CLI ticks up smoothly.
    Cumulative, monotonic, and input stays 0 until a server usage arrives.
    """
    provider = OpenAIProvider(model="gpt-4o", api_key="test-key")
    chunks = [
        _make_chunk(content="Hello"),
        _make_chunk(content=" world"),
        _make_chunk(content="!"),
    ]
    with patch.object(
        provider._client.chat.completions, "create",
        new=MagicMock(return_value=_chunk_stream(chunks)),
    ):
        out = [c async for c in provider.stream(messages=[Message(role="user", content="Hi")])]

    usage_chunks = [c for c in out if c.usage]
    assert len(usage_chunks) == 3
    outputs = [c.usage["output"] for c in usage_chunks]
    assert outputs == sorted(outputs)  # monotonic, non-decreasing
    assert outputs[-1] > 0
    assert all(c.usage["input"] == 0 for c in usage_chunks)
