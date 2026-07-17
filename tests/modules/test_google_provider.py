"""Tests for GoogleProvider — mock-based, no real API calls."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from synapse.modules.providers.google import GoogleProvider
from synapse.protocols.llm import Message, LLMResponse


@pytest.fixture
def provider():
    return GoogleProvider(model="gemini-pro", api_key="test-key")


def test_model_id(provider):
    assert provider.model_id == "gemini-pro"


@pytest.mark.asyncio
async def test_chat_basic():
    provider = GoogleProvider(model="gemini-pro", api_key="test-key")

    # Build a mock Gemini response
    from google.genai import types

    mock_response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model",
                    parts=[types.Part.from_text(text="Hello from Gemini")],
                ),
                finish_reason=types.FinishReason.STOP,
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=10,
            candidates_token_count=5,
        ),
    )

    with patch.object(
        provider._client.aio.models, "generate_content", new=AsyncMock(return_value=mock_response)
    ):
        result = await provider.chat(
            messages=[Message(role="user", content="Hi")],
        )

    assert isinstance(result, LLMResponse)
    assert result.content == "Hello from Gemini"
    assert result.stop_reason == "end_turn"
    assert result.usage["input"] == 10
    assert result.usage["output"] == 5
