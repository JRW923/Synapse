"""Tests for GoogleProvider — mock-based, no real API calls."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from synapse.protocols.llm import Message, LLMResponse

# Google provider may not be available (google-genai not installed)
try:
    from synapse.modules.providers.google import GoogleProvider
    if GoogleProvider is not None:
        from synapse.modules.providers.google import genai_types as gtypes
        _GOOGLE_AVAILABLE = True
    else:
        gtypes = None
        _GOOGLE_AVAILABLE = False
except ImportError:
    GoogleProvider = None  # type: ignore
    gtypes = None
    _GOOGLE_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _GOOGLE_AVAILABLE, reason="google-genai package is not installed")


@pytest.fixture
def provider():
    return GoogleProvider(model="gemini-pro", api_key="test-key")


def test_model_id(provider):
    assert provider.model_id == "gemini-pro"


@pytest.mark.asyncio
async def test_chat_basic():
    provider = GoogleProvider(model="gemini-pro", api_key="test-key")

    # Build a mock Gemini response
    mock_response = gtypes.GenerateContentResponse(
        candidates=[
            gtypes.Candidate(
                content=gtypes.Content(
                    role="model",
                    parts=[gtypes.Part.from_text(text="Hello from Gemini")],
                ),
                finish_reason=gtypes.FinishReason.STOP,
            )
        ],
        usage_metadata=gtypes.GenerateContentResponseUsageMetadata(
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
