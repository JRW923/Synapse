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


@pytest.mark.asyncio
async def test_swarm_tracker_renders_lifecycle():
    """_SwarmTracker turns swarm events into compact panel lines and cleans up."""
    from synapse.adapters.cli import _SwarmTracker
    from synapse.protocols.events import (
        WorkerSpawned, WorkerCompleted, ReviewSubmitted, SwarmVerified,
    )

    updates = []
    tracker = _SwarmTracker(updates.append)
    bus = EventBus()
    tracker.wire(bus)

    await bus.emit(WorkerSpawned(session_id="s1", agent_id="w1", role="coder", task="x"))
    await bus.emit(WorkerCompleted(session_id="s1", agent_id="w1", role="coder", status="success"))
    await bus.emit(ReviewSubmitted(session_id="s1", agent_id="w2", reviewer_role="reviewer",
                                   target_role="coder", verdict="reject", comments="nope"))
    await bus.emit(SwarmVerified(session_id="s1", status="partial", issues="i"))

    joined = "\n".join(tracker.render_lines())
    assert "coder" in joined
    assert "rejected=1" in joined
    assert "verified: partial" in joined
    # on_update fired once per event (spawn, complete, review, verify).
    assert len(updates) == 4

    tracker.unwire(bus)
    assert all(len(v) == 0 for v in bus._handlers.values())


def test_friendly_error_maps_synapse_errors():
    """L.5 — SynapseError subclasses render as 中文 原因+建议, no traceback."""
    from synapse.adapters.cli import _friendly_error
    from synapse.core.exceptions import (
        ProviderError, ConfigError, ToolError, SandboxError, PlannerError,
    )

    for exc in (
        ProviderError("401 auth"),
        ConfigError("bad yaml"),
        ToolError("boom"),
        SandboxError("blocked"),
        PlannerError("loop"),
    ):
        out = _friendly_error(exc)
        assert "原因：" in out and "建议：" in out
        assert "Traceback" not in out

    # A plain (non-Synapse) error still hides the traceback and gives a hint.
    assert "原因：" in _friendly_error(RuntimeError("kaboom"))
    assert "建议：" in _friendly_error(RuntimeError("kaboom"))
