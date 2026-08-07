"""Tests for the CLI streaming helper (TODO L.1)."""

import io

import pytest
from unittest.mock import AsyncMock

from synapse.protocols.planner import ResultStatus, AgentResult, ExecutionMetrics
from synapse.core.events import EventBus
from synapse.protocols.events import AgentProgress, LLMToken


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
async def test_run_task_streamed_increments_tokens_from_stream():
    """End-to-end: streamed per-chunk usage must tick the token counter up
    (12 = 10 in + 2 out) instead of staying at 0 until the final 'tokens='."""
    from synapse.adapters.cli import _run_task_streamed
    from rich.console import Console

    bus = EventBus()

    class _Container:
        def resolve(self, _t):
            return bus

    async def _run(task, session=None):
        await bus.emit(AgentProgress(session_id="s", phase="calling_llm", message="calling"))
        await bus.emit(LLMToken(session_id="s", text="Hi", usage={"input": 10, "output": 1}))
        await bus.emit(LLMToken(session_id="s", text=" there", usage={"input": 10, "output": 2}))
        # Authoritative reconciliation at end of request.
        await bus.emit(AgentProgress(session_id="s", phase="token_update", message="tokens=10+2"))
        return AgentResult(status=ResultStatus.SUCCESS, output="done", metrics=ExecutionMetrics())

    class _Syn:
        _container = _Container()
        run = staticmethod(_run)

    console = Console(file=io.StringIO(), force_terminal=True, width=80)
    result = await _run_task_streamed(_Syn(), "t", None, console, True)
    assert result.status.value == "success"
    assert "12 tok" in console.file.getvalue()


@pytest.mark.asyncio
async def test_run_task_streamed_resets_baseline_per_request():
    """A second request must increment from the first request's total (no
    double-count, no reset to zero)."""
    from synapse.adapters.cli import _run_task_streamed
    from rich.console import Console

    bus = EventBus()

    class _Container:
        def resolve(self, _t):
            return bus

    async def _run(task, session=None):
        # request 1: 10 in + 2 out -> 12
        await bus.emit(AgentProgress(session_id="s", phase="calling_llm", message="c1"))
        await bus.emit(LLMToken(session_id="s", text="a", usage={"input": 10, "output": 2}))
        await bus.emit(AgentProgress(session_id="s", phase="token_update", message="tokens=10+2"))
        # request 2: 5 in + 3 out -> baseline(12) + 8 = 20
        await bus.emit(AgentProgress(session_id="s", phase="calling_llm", message="c2"))
        await bus.emit(LLMToken(session_id="s", text="b", usage={"input": 5, "output": 3}))
        await bus.emit(AgentProgress(session_id="s", phase="token_update", message="tokens=15+5"))
        return AgentResult(status=ResultStatus.SUCCESS, output="done", metrics=ExecutionMetrics())

    class _Syn:
        _container = _Container()
        run = staticmethod(_run)

    console = Console(file=io.StringIO(), force_terminal=True, width=80)
    await _run_task_streamed(_Syn(), "t", None, console, True)
    # Final authoritative total is 15 in + 5 out = 20; counter must reach it
    # via baseline + streamed usage, not reset per request.
    assert "20 tok" in console.file.getvalue()


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


def test_main_no_subcommand_does_not_crash_on_optional_args(monkeypatch):
    """`synapse` with no subcommand must not AttributeError on provider/model/mode,
    which only exist on the run/chat subparsers (regression for the top-level
    Namespace missing those attributes)."""
    from synapse.adapters import cli

    captured = {}

    async def fake_main(config_path=None, resume=None, provider=None, model=None, mode=None):
        captured["provider"] = provider
        captured["model"] = model
        captured["mode"] = mode

    monkeypatch.setattr(cli, "_main_interface", fake_main)
    monkeypatch.setattr("sys.argv", ["synapse"])

    cli.main()

    assert captured == {"provider": None, "model": None, "mode": None}
