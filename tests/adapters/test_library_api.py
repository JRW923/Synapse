"""Tests for the Synapse library API facade."""

import pytest
from unittest.mock import AsyncMock, patch

from synapse.protocols.llm import LLMResponse
from synapse.protocols.planner import ResultStatus
from synapse.core.events import EventBus
from synapse.protocols.events import FileWritten


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
        "synapse.modules.providers.anthropic.AnthropicProvider",
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
    with patch("synapse.modules.providers.anthropic.AnthropicProvider"):
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


# ---------------------------------------------------------------------------
# TODO K — runtime scoring closed loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_score_populated_after_run():
    """run() resets the collectors, then exposes a score with all four categories."""
    mock_llm = AsyncMock()
    mock_llm.model_id = "mock"
    mock_llm.chat.return_value = LLMResponse(
        content="Task completed successfully.",
        tool_calls=[],
        stop_reason="end_turn",
        usage={"input": 10, "output": 5},
    )

    with patch(
        "synapse.modules.providers.anthropic.AnthropicProvider",
        return_value=mock_llm,
    ):
        from synapse.adapters.library import Synapse

        synapse = Synapse(provider="anthropic")
        result = await synapse.run("Say hello")

    score = synapse.get_run_score()
    assert score is not None
    assert set(score.keys()) == {"task", "status", "safety", "process", "quality", "efficiency"}
    assert score["status"] == "success"
    # Every category is present and itself a dict of metrics.
    for cat in ("safety", "process", "quality", "efficiency"):
        assert isinstance(score[cat], dict) and score[cat]


@pytest.mark.asyncio
async def test_run_metrics_wired_and_collect():
    """Collectors are subscribed to the EventBus, so real events update the score."""
    mock_llm = AsyncMock()
    mock_llm.model_id = "mock"
    mock_llm.chat.return_value = LLMResponse(
        content="done", tool_calls=[], stop_reason="end_turn", usage={"input": 1, "output": 1},
    )

    with patch(
        "synapse.modules.providers.anthropic.AnthropicProvider",
        return_value=mock_llm,
    ):
        from synapse.adapters.library import Synapse

        synapse = Synapse(provider="anthropic")
        bus = synapse._container.resolve(EventBus)

        # Before any event: fresh collectors show no out-of-workspace access.
        assert synapse.get_run_score()["safety"]["out_of_workspace_access"] == 0

        # Emit a real out-of-workspace write → SafetyMetrics must pick it up.
        await bus.emit(FileWritten(session_id="s1", path="/etc/passwd", bytes_written=100))

        score = synapse.get_run_score()
        assert score["safety"]["out_of_workspace_access"] >= 1
