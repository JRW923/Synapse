"""Tests for OpenAIProvider — mock-based, no real API calls."""
import json
import pytest
from unittest.mock import AsyncMock, patch
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
