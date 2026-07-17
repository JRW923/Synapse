"""Tests for the Synapse library API facade."""

import pytest
from unittest.mock import AsyncMock, patch

from synapse.protocols.llm import LLMResponse
from synapse.protocols.planner import ResultStatus


# ---------------------------------------------------------------------------
# test_library_api_basic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_library_api_basic():
    """A simple task completes successfully with a mocked LLM."""
    mock_llm = AsyncMock()
    mock_llm.model_id = "mock"
    mock_llm.chat.return_value = LLMResponse(
        content="Task completed successfully.",
        tool_calls=[],
        stop_reason="end_turn",
        usage={"input": 10, "output": 5},
    )

    with patch(
        "synapse.adapters.library.AnthropicProvider",
        return_value=mock_llm,
    ):
        from synapse.adapters.library import Synapse

        synapse = Synapse(provider="anthropic")
        result = await synapse.run("Say hello")

    assert result.status == ResultStatus.SUCCESS
    assert "Task completed" in result.output
    assert result.metrics.tokens_input == 10
    assert result.metrics.tokens_output == 5


# ---------------------------------------------------------------------------
# test_library_api_config_override
# ---------------------------------------------------------------------------

def test_library_api_config_override():
    """**overrides passed to Synapse(...) take precedence over defaults."""
    with patch("synapse.adapters.library.AnthropicProvider"):
        from synapse.adapters.library import Synapse

        synapse = Synapse(
            provider="anthropic",
            model="claude-opus-4-6",
            max_tokens=8000,
            max_iterations=100,
        )

    config = synapse._config
    assert config.provider.provider == "anthropic"
    assert config.provider.model == "claude-opus-4-6"
    assert config.provider.max_tokens == 8000
    assert config.planning.max_iterations == 100
