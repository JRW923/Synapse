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
    assert set(score.keys()) == {"task", "status", "safety", "process", "quality", "efficiency", "process_hint"}
    assert score["status"] == "success"
    # Every category is present and itself a dict of metrics.
    for cat in ("safety", "process", "quality", "efficiency"):
        assert isinstance(score[cat], dict) and score[cat]
    # L.4 — a real run emits ProcessQualityScored, so the closed-loop hint is
    # surfaced (non-empty string).
    assert isinstance(score["process_hint"], str) and score["process_hint"]


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


@pytest.mark.asyncio
async def test_run_score_includes_process_hint():
    """L.4 — the last ProcessQualityScored hint is surfaced via get_run_score."""
    from synapse.protocols.events import ProcessQualityScored
    from synapse.protocols.llm import LLMResponse

    mock_llm = AsyncMock()
    mock_llm.model_id = "mock"
    mock_llm.chat.return_value = LLMResponse(
        content="Task completed successfully.", tool_calls=[],
        stop_reason="end_turn", usage={"input": 10, "output": 5},
    )

    with patch(
        "synapse.modules.providers.anthropic.AnthropicProvider",
        return_value=mock_llm,
    ):
        from synapse.adapters.library import Synapse

        synapse = Synapse(provider="anthropic")
        bus = synapse._container.resolve(EventBus)

        # No hint yet.
        assert synapse.get_run_score()["process_hint"] is None

        await bus.emit(ProcessQualityScored(
            session_id="s1", task="t", score=0.1, reuse_ratio=0.0, write_without_lookup=3,
            thrashing_events=0, success=True, tool_calls=5,
            hint="下次请先 grep/read 定位可复用代码。",
        ))

        assert synapse.get_run_score()["process_hint"] == "下次请先 grep/read 定位可复用代码。"
        # A subsequent run resets the hint, and the closed loop re-captures it.
        await synapse.run("Say hello")
        assert isinstance(synapse.get_run_score()["process_hint"], str) and synapse.get_run_score()["process_hint"]
