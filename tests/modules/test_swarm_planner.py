"""Tests for the Swarm / Team planner (TODO C).

Verifies: parallel coder execution, per-worker session isolation, swarm-level
events carry agent_id/role, review+vote emission, and the verify loop that
re-runs the weakest coder on a reject (bounded by max_verify_loops).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from synapse.protocols.planner import AgentResult, ResultStatus, PlanningMode
from synapse.protocols.llm import LLMResponse
from synapse.protocols.events import (
    WorkerSpawned, WorkerCompleted, ReviewSubmitted, VoteCast, SwarmVerified,
)
from synapse.core.events import EventBus
from synapse.modules.planning.swarm import SwarmPlanner, RoleSpec, FilteredToolRegistry
from synapse.modules.security.auth import ActionAuthorizer
from pathlib import Path


def _result(output, status=ResultStatus.SUCCESS):
    return AgentResult(status=status, output=output)


class _FakePlanner:
    """Stand-in worker planner; execute returns a controllable AgentResult."""

    def __init__(self, side_effect):
        self.execute = AsyncMock(side_effect=side_effect)
        self.mode = PlanningMode.REACT


class _FakeTools:
    _NAMES = ("read", "grep", "glob", "git", "write", "edit")

    def get(self, name: str):
        if name in self._NAMES:
            return object()
        raise KeyError(f"no such tool: {name}")

    def get_schemas(self) -> list[dict]:
        return [{"name": n} for n in self._NAMES]


def _fake_tools():
    return _FakeTools()


def _run_swarm(roles, llm, planner_factory, session_id="sess-1"):
    bus = EventBus()
    captured = []

    # EventBus awaits handlers, so the capture callback must be async.
    async def _capture(e, et):
        captured.append((et, e))

    for et in (WorkerSpawned, WorkerCompleted, ReviewSubmitted, VoteCast, SwarmVerified):
        bus.subscribe(et.event_type, lambda e, et=et: _capture(e, et))
    session = MagicMock()
    session.id = session_id
    session.fork.return_value = MagicMock()
    planner = SwarmPlanner(react_planner=MagicMock(), roles=roles, planner_factory=planner_factory)
    ctx = SimpleNamespace()
    result = asyncio.run(planner.execute(
        task="实现功能 X", context=ctx, tools=_fake_tools(), llm=llm,
        sandbox=MagicMock(), session=session, event_bus=bus,
    ))
    return result, captured, session


def test_happy_path_approve():
    roles = [
        RoleSpec(role="coder", count=1),
        RoleSpec(role="reviewer", tool_filter={"read"}),
    ]
    llm = AsyncMock()
    # count==1 → no decompose; only the merge calls the LLM (reviewer is mocked).
    llm.chat.side_effect = [LLMResponse("merged ok")]

    created = {}
    def factory(spec):
        if spec.role == "reviewer":
            p = _FakePlanner([_result("通过，满足任务")])
        else:
            p = _FakePlanner([_result("coder output")])
        created[spec.role] = p
        return p

    result, captured, session = _run_swarm(roles, llm, factory)

    # Events carry agent_id/role and the right lifecycle.
    types = [t for t, _ in captured]
    assert types.count(WorkerSpawned) == 2
    assert types.count(WorkerCompleted) == 2
    assert types.count(ReviewSubmitted) == 1
    review = [e for t, e in captured if t is ReviewSubmitted][0]
    assert review.verdict == "approve"
    assert types.count(VoteCast) == 1
    verified = [e for t, e in captured if t is SwarmVerified][0]
    assert verified.status == "success"

    # Result carries swarm attribution + all contributors.
    assert result.role == "swarm" and result.agent_id == "swarm"
    assert len(result.contributors) == 2

    # Coders ran in parallel (gather); reviewer after; each once.
    assert created["coder"].execute.call_count == 1
    assert created["reviewer"].execute.call_count == 1
    assert llm.chat.call_count == 1
    # Each worker got an isolated forked session.
    assert session.fork.call_count == 2


def test_coders_run_in_parallel():
    roles = [RoleSpec(role="coder", count=2)]
    llm = AsyncMock()
    # decompose (1) + merge (1) = 2 LLM calls (no reviewer).
    llm.chat.side_effect = [
        LLMResponse('[{"id":"1","description":"a","file_scope":"src/a.py"},'
                    '{"id":"2","description":"b","file_scope":"src/b.py"}]'),
        LLMResponse("merged"),
    ]

    starts, ends = [], []
    async def coder_run(**kw):
        starts.append(time.monotonic())
        await asyncio.sleep(0.05)
        ends.append(time.monotonic())
        return _result("coder")

    def factory(spec):
        return _FakePlanner(coder_run)

    _run_swarm(roles, llm, factory)

    # All coders started before any of them finished → genuine concurrency.
    assert max(starts) < min(ends)


def test_verify_loop_reruns_on_reject():
    roles = [
        RoleSpec(role="coder", count=1),
        RoleSpec(role="reviewer", tool_filter={"read"}),
    ]
    llm = AsyncMock()
    # merge + re-merge = 2 LLM calls (reviewer text comes from the mock planner).
    llm.chat.side_effect = [LLMResponse("merged v1"), LLMResponse("merged v2")]

    # Each role's planner is created ONCE and reused for the retry, so the
    # side_effect must be a *list* that advances across calls: the reviewer
    # rejects first, then approves; the coder's first attempt fails the review,
    # the retry passes.
    side_effects = {
        "coder": [_result("coder v1"), _result("coder v2 fixed")],
        "reviewer": [_result("不通过，缺少错误处理"), _result("通过")],
    }
    created = {}
    def factory(spec):
        p = _FakePlanner(side_effects[spec.role])
        created[spec.role] = p
        return p

    result, captured, _ = _run_swarm(roles, llm, factory)

    verified = [e for t, e in captured if t is SwarmVerified][0]
    assert verified.status == "success"  # recovered after one retry

    # Initial coder run + one retry; initial review + one re-review.
    assert created["coder"].execute.call_count == 2
    assert created["reviewer"].execute.call_count == 2
    # Exactly one reject then one approve.
    reviews = [e.verdict for t, e in captured if t is ReviewSubmitted]
    assert reviews == ["reject", "approve"]


def test_filtered_tool_registry():
    inner = _fake_tools()
    filtered = FilteredToolRegistry(inner, {"read", "grep"})
    # Allowed tools resolve; disallowed raise.
    assert filtered.get("read") is not None
    try:
        filtered.get("write")
        assert False, "expected KeyError"
    except KeyError:
        pass
    names = {s["name"] for s in filtered.get_schemas()}
    assert names == {"read", "grep"}


def test_spawn_stores_file_scope():
    swarm = SwarmPlanner(react_planner=MagicMock(), roles=[RoleSpec(role="coder", file_scope=True, count=2)])
    spec = RoleSpec(role="coder", file_scope=True)
    w = swarm._spawn(spec, "coder-1", "task", MagicMock(), MagicMock(), file_scope="src/a.py")
    assert w["file_scope"] == "src/a.py"
    # Default (no scope) → empty string, no regression.
    w2 = swarm._spawn(spec, "coder-2", "task", MagicMock(), MagicMock())
    assert w2["file_scope"] == ""


def test_coder_file_scope_threaded_to_per_worker_auth():
    """Each coder with a scope gets its own ActionAuthorizer with that scope as
    a write allow-list; an empty scope reuses the shared base authorizer."""
    base_auth = ActionAuthorizer(workspace_root="/project", confirmation_enabled=True)
    base = SimpleNamespace(
        auth=base_auth,
        max_iterations=10, thrashing_threshold=3, max_thrashing_events=3,
        max_tokens_per_task=1000, _confirm=None, total_timeout_seconds=120,
    )
    swarm = SwarmPlanner(react_planner=base, roles=[RoleSpec(role="coder", file_scope=True, count=2)])

    scoped = swarm._make_planner(RoleSpec(role="coder", file_scope=True), file_scope="src/a.py")
    assert scoped.auth is not base_auth
    assert len(scoped.auth._allowed_paths) == 1
    # A file-named scope is normalized to its containing directory boundary.
    assert scoped.auth._allowed_paths[0] == Path("/project/src").resolve()

    # Reviewer / no-scope path keeps the shared base authorizer (no whitelist).
    unscoped = swarm._make_planner(RoleSpec(role="coder", file_scope=True), file_scope="")
    assert unscoped.auth is base_auth
