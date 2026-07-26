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


def _make_gchunk(text=None, function_call=None):
    part = type("Part", (), {"text": text, "function_call": function_call})()
    content = type("Content", (), {"parts": [part]})()
    candidate = type("Candidate", (), {"content": content})()
    return type("Chunk", (), {"candidates": [candidate]})()


async def _gchunk_stream(chunks):
    for c in chunks:
        yield c


def _make_gchunk_with_usage(text, prompt_tokens, candidates_tokens):
    """A Gemini stream chunk whose usage_metadata carries running totals."""
    part = type("Part", (), {"text": text, "function_call": None})()
    content = type("Content", (), {"parts": [part]})()
    candidate = type("Candidate", (), {"content": content})()
    usage = type(
        "UM", (),
        {"prompt_token_count": prompt_tokens, "candidates_token_count": candidates_tokens},
    )()
    return type("Chunk", (), {"candidates": [candidate], "usage_metadata": usage})()


@pytest.mark.asyncio
async def test_stream_emits_incremental_usage():
    """Regression: Gemini exposes running token totals on each chunk's
    usage_metadata; the provider must emit them as they arrive so the CLI
    counter ticks up during generation (not only at the end)."""
    provider = GoogleProvider(model="gemini-pro", api_key="test-key")
    chunks = [
        _make_gchunk_with_usage("Hello", 10, 1),
        _make_gchunk_with_usage(" world", 10, 2),
        _make_gchunk_with_usage("!", 10, 3),
    ]
    with patch.object(
        provider._client.aio.models, "generate_content_stream",
        new=AsyncMock(return_value=_gchunk_stream(chunks)),
    ):
        out = [c async for c in provider.stream(messages=[Message(role="user", content="Hi")])]

    usage_chunks = [c for c in out if c.usage]
    assert [c.usage["output"] for c in usage_chunks] == [1, 2, 3]
    assert all(c.usage["input"] == 10 for c in usage_chunks)


@pytest.mark.asyncio
async def test_stream_yields_text_and_tool_chunks():
    provider = GoogleProvider(model="gemini-pro", api_key="test-key")
    chunks = [
        _make_gchunk(text="Hello"),
        _make_gchunk(text=" world"),
        _make_gchunk(function_call=type("FC", (), {"name": "read", "args": {"path": "/x"}})()),
    ]
    with patch.object(
        provider._client.aio.models, "generate_content_stream",
        new=AsyncMock(return_value=_gchunk_stream(chunks)),
    ):
        out = [c async for c in provider.stream(messages=[Message(role="user", content="Hi")])]

    assert [c.content for c in out] == ["Hello", " world", ""]
    assert out[2].tool_call_delta is not None
    assert out[2].tool_call_delta["name"] == "read"


@pytest.mark.asyncio
async def test_convert_messages_roundtrips_tool_calls():
    """Regression: multi-turn tool calls must survive conversion.

    Before the fix, assistant tool_calls were dropped (only text kept) and
    tool-result responses used an empty name — breaking Gemini multi-turn
    tool use.  Now each assistant call becomes a function_call part and each
    tool result recovers its name from the preceding call's tool_call_id.
    """
    provider = GoogleProvider(model="gemini-pro", api_key="test-key")
    messages = [
        Message(role="user", content="List files"),
        Message(
            role="assistant",
            content="",
            tool_calls=[{"id": "call_1", "name": "grep", "input": {"pattern": "*.py"}}],
        ),
        Message(role="tool", tool_call_id="call_1", content='["a.py", "b.py"]'),
    ]

    contents = provider._convert_messages(messages)

    # user, assistant (model), tool — 3 contents
    assert len(contents) == 3
    model_parts = contents[1].parts
    # assistant turn must carry the function_call (not just empty text)
    fc_parts = [p for p in model_parts if getattr(p, "function_call", None)]
    assert len(fc_parts) == 1
    assert fc_parts[0].function_call.name == "grep"
    assert fc_parts[0].function_call.args == {"pattern": "*.py"}

    tool_parts = contents[2].parts
    fr_parts = [p for p in tool_parts if getattr(p, "function_response", None)]
    assert len(fr_parts) == 1
    # name must be recovered, not empty
    assert fr_parts[0].function_response.name == "grep"
