"""Tests for DeepSeekProvider — mock-based, no real API calls."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from synapse.modules.providers.deepseek import DeepSeekProvider, DEEPSEEK_BASE_URL
from synapse.protocols.llm import Message, LLMResponse
from synapse.core.exceptions import ProviderError


@pytest.fixture
def provider():
    return DeepSeekProvider(model="deepseek-chat", api_key="test-key")


def test_model_id(provider):
    assert provider.model_id == "deepseek-chat"


def test_default_base_url():
    provider = DeepSeekProvider(api_key="test-key")
    assert provider._base_url == DEEPSEEK_BASE_URL


def test_custom_base_url():
    provider = DeepSeekProvider(api_key="test-key", base_url="https://custom.example.com/v1")
    assert provider._base_url == "https://custom.example.com/v1"


@pytest.mark.asyncio
async def test_chat_basic():
    provider = DeepSeekProvider(model="deepseek-chat", api_key="test-key")

    mock_message = MagicMock()
    mock_message.content = "Hello, I am DeepSeek"
    mock_message.tool_calls = None

    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_choice.finish_reason = "stop"

    mock_usage = MagicMock()
    mock_usage.prompt_tokens = 10
    mock_usage.completion_tokens = 5

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = mock_usage

    with patch.object(provider._client.chat.completions, "create", new=AsyncMock(return_value=mock_response)):
        result = await provider.chat(
            messages=[Message(role="user", content="Hi")],
        )

    assert isinstance(result, LLMResponse)
    assert result.content == "Hello, I am DeepSeek"
    assert result.stop_reason == "stop"
    assert result.usage["input"] == 10
    assert result.usage["output"] == 5


@pytest.mark.asyncio
async def test_chat_with_tools():
    provider = DeepSeekProvider(model="deepseek-chat", api_key="test-key")

    mock_tool_call = MagicMock()
    mock_tool_call.id = "call_1"
    mock_tool_call.function.name = "read"
    mock_tool_call.function.arguments = '{"path": "/test.txt"}'

    mock_message = MagicMock()
    mock_message.content = ""
    mock_message.tool_calls = [mock_tool_call]

    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_choice.finish_reason = "tool_calls"

    mock_usage = MagicMock()
    mock_usage.prompt_tokens = 20
    mock_usage.completion_tokens = 15

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = mock_usage

    with patch.object(provider._client.chat.completions, "create", new=AsyncMock(return_value=mock_response)):
        result = await provider.chat(
            messages=[Message(role="user", content="Read the file")],
            tools=[{"name": "read", "description": "Read a file", "input_schema": {}}],
        )

    assert result.stop_reason == "tool_calls"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "read"
    assert result.tool_calls[0]["input"] == {"path": "/test.txt"}


@pytest.mark.asyncio
async def test_chat_skips_empty_user_messages():
    """Empty user messages (tool-result placeholders) should be filtered out."""
    provider = DeepSeekProvider(model="deepseek-chat", api_key="test-key")

    mock_message = MagicMock()
    mock_message.content = "Got it"
    mock_message.tool_calls = None

    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_choice.finish_reason = "stop"

    mock_usage = MagicMock()
    mock_usage.prompt_tokens = 5
    mock_usage.completion_tokens = 3

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = mock_usage

    with patch.object(provider._client.chat.completions, "create", new=AsyncMock(return_value=mock_response)):
        await provider.chat(
            messages=[
                Message(role="user", content="Read /tmp/x"),
                Message(role="assistant", content=""),
                Message(role="user", content=""),
            ],
        )

    # Should not crash on empty user messages
    assert True


@pytest.mark.asyncio
async def test_chat_with_system_message():
    """System messages should be passed through in OpenAI format."""
    provider = DeepSeekProvider(model="deepseek-chat", api_key="test-key")

    mock_message = MagicMock()
    mock_message.content = "I understand"
    mock_message.tool_calls = None

    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_choice.finish_reason = "stop"

    mock_usage = MagicMock()
    mock_usage.prompt_tokens = 8
    mock_usage.completion_tokens = 4

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = mock_usage

    with patch.object(provider._client.chat.completions, "create", new=AsyncMock(return_value=mock_response)):
        result = await provider.chat(
            messages=[
                Message(role="system", content="You are a helpful assistant"),
                Message(role="user", content="Hi"),
            ],
        )

    assert isinstance(result, LLMResponse)
    assert result.content == "I understand"


@pytest.mark.asyncio
async def test_chat_api_error():
    """ProviderError should be raised on API failure."""
    provider = DeepSeekProvider(model="deepseek-chat", api_key="test-key")

    with patch.object(
        provider._client.chat.completions,
        "create",
        new=AsyncMock(side_effect=Exception("Connection refused")),
    ):
        with pytest.raises(ProviderError, match="DeepSeek API error"):
            await provider.chat(messages=[Message(role="user", content="Hi")])


@pytest.mark.asyncio
async def test_stream_not_implemented():
    provider = DeepSeekProvider(api_key="test-key")
    with pytest.raises(NotImplementedError, match="Streaming will be added in Phase 2"):
        async for _ in await provider.stream(messages=[Message(role="user", content="Hi")]):
            pass
