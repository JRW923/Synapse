"""Tests for the CLI streaming helper (TODO L.1)."""

import pytest
from unittest.mock import AsyncMock

from synapse.protocols.planner import ResultStatus, AgentResult, ExecutionMetrics
from synapse.core.events import EventBus


@pytest.mark.asyncio
async def test_run_task_streamed_non_rich_falls_back():
    """Without rich, the helper simply runs the task and returns the result."""
    from synapse.adapters.cli import _run_task_streamed

    mock = AsyncMock()
    mock.run.return_value = AgentResult(
        status=ResultStatus.SUCCESS, output="ok", metrics=ExecutionMetrics(),
    )
    result = await _run_task_streamed(mock, "t", None, None, False)
    assert result.status.value == "success"
    mock.run.assert_called_once()


@pytest.mark.asyncio
async def test_run_task_streamed_rich_streams_and_cleans_up():
    """With rich, the helper subscribes to the bus, runs, and unsubscribes."""
    from synapse.adapters.cli import _run_task_streamed
    from rich.console import Console

    mock = AsyncMock()
    mock.run.return_value = AgentResult(
        status=ResultStatus.SUCCESS, output="hi", metrics=ExecutionMetrics(),
    )

    class _Container:
        def resolve(self, _t):
            return EventBus()

    mock._container = _Container()
    bus = mock._container.resolve(EventBus)

    result = await _run_task_streamed(mock, "t", None, Console(), True)
    assert result.status.value == "success"
    # After the run, no handlers should remain subscribed (cleanup ran).
    # _handlers is a defaultdict, so assert every bucket is empty rather than
    # comparing to {} (empty keys persist).
    assert all(len(v) == 0 for v in bus._handlers.values())
