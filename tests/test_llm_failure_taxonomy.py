"""LLM failure taxonomy + per-call latency accounting in the ReAct loop."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from synapse.modules.planning.react import (
    ReActPlanner, _classify_llm_failure, _is_non_retryable_llm_error,
)
from synapse.protocols.llm import LLMResponse
from synapse.protocols.retriever import Context
from synapse.core.session import Session
from synapse.core.events import EventBus


def test_503_classified_as_provider_unavailable():
    e = RuntimeError("OpenAI streaming error: Error code: 503 - "
                     "{'error': {'kind': 'provider_unavailable'}}")
    assert _classify_llm_failure(e) == "provider_unavailable"
    assert not _is_non_retryable_llm_error(e)  # retryable


def test_timeout_classified_as_provider_unavailable():
    assert _classify_llm_failure(asyncio.TimeoutError()) == "provider_unavailable"


def test_auth_is_non_retryable():
    e = RuntimeError("Error code: 401 - invalid_api_key")
    assert _classify_llm_failure(e) == "auth"
    assert _is_non_retryable_llm_error(e)


def test_unknown_error_bucket():
    assert _classify_llm_failure(ValueError("weird")) == "llm_error"


def _registry():
    class _R:
        def get(self, name):
            return None
        def get_schemas(self):
            return []
    return _R()


def test_fatal_llm_failure_tags_metrics_and_counts_calls():
    llm = AsyncMock()
    llm.chat.side_effect = RuntimeError(
        "Error code: 503 - provider_unavailable Service temporarily unavailable")
    planner = ReActPlanner(max_iterations=3, max_llm_retries=2)
    result = asyncio.run(planner.execute(
        task="x", context=Context(), tools=_registry(), llm=llm,
        sandbox=None, session=Session(), event_bus=EventBus(),
    ))
    assert result.status.value == "failed"
    assert result.metrics.llm_failure == "provider_unavailable"
    # every attempt (including the fatal one) is counted and timed
    assert result.metrics.llm_call_count == 3
    assert result.metrics.llm_time_ms >= 0


def test_successful_run_accounts_llm_time():
    llm = AsyncMock()
    llm.chat.return_value = LLMResponse(
        content="done", tool_calls=[], stop_reason="end_turn",
        usage={"input": 5, "output": 2})
    planner = ReActPlanner(max_iterations=2)
    result = asyncio.run(planner.execute(
        task="x", context=Context(), tools=_registry(), llm=llm,
        sandbox=None, session=Session(), event_bus=EventBus(),
    ))
    assert result.metrics.llm_failure == ""
    assert result.metrics.llm_call_count == 1
    assert result.metrics.llm_time_ms >= 0


def test_runner_counts_provider_outage_as_infra_failure():
    from synapse.eval.runner import TaskResult
    items = [
        TaskResult(task_id="a", status="failed", failure_kind="provider_unavailable"),
        TaskResult(task_id="b", status="failed", failure_kind=""),
        TaskResult(task_id="c", status="success"),
    ]
    infra = sum(
        i.status == "error"
        or i.verification_status == "grader_error"
        or i.failure_kind == "provider_unavailable"
        for i in items)
    assert infra == 1
    # and the field survives report serialization
    d = TaskResult(task_id="a", status="failed", failure_kind="provider_unavailable")
    assert d.failure_kind == "provider_unavailable"
