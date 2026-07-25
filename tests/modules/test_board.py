"""Tests for s12 TaskBoard + s17 自主认领.

验收：board 投放 5 任务，2 worker 并发认领，无重复认领、全部完成（s17）；
单 agent 串行认领可见状态（s12）；状态事件正确发出；Swarm 自主模式接入板。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from synapse.modules.planning.board import TaskBoard, TaskStatus
from synapse.modules.planning.swarm import SwarmPlanner, RoleSpec
from synapse.modules.security.auth import ActionAuthorizer
from synapse.protocols.planner import ResultStatus, PlanningMode
from synapse.protocols.llm import LLMResponse
from synapse.core.events import EventBus


def _board_result(output="out"):
    return type("R", (), {
        "status": ResultStatus.SUCCESS, "output": output,
        "metrics": SimpleNamespace(
            tokens_input=0, tokens_output=0, tool_call_count=0,
            tool_success_count=0, thrashing_events=0,
        ),
    })()


async def test_concurrent_claim_no_duplicates():
    """s17 验收：5 任务、2 worker 并发，无重复认领，全部完成。"""
    board = TaskBoard(event_bus=EventBus(), session_id="s")
    await board.add_many([(f"t{i}", f"desc{i}") for i in range(5)])

    claimed = []

    async def worker(wid):
        while True:
            t = await board.claim(wid)
            if t is None:
                return
            claimed.append(t.id)
            await board.complete(t.id)

    await asyncio.gather(*[worker(f"w{i}") for i in range(2)])

    assert sorted(claimed) == [f"t{i}" for i in range(5)]
    assert len(claimed) == 5            # 每个任务恰好被认领一次
    assert board.all_done()


async def test_single_agent_serial_claim():
    """s12 验收：单 agent 串行认领所有子任务，状态可见。"""
    board = TaskBoard()
    await board.add_many([(f"s{i}", f"d{i}") for i in range(3)])

    done = []

    async def agent():
        while True:
            t = await board.claim("agent-1")
            if t is None:
                return
            done.append(t.id)
            await board.complete(t.id)

    await agent()
    assert done == ["s0", "s1", "s2"]
    assert board.pending() == []
    assert board.all_done()


async def test_status_events_emitted():
    """状态变更 / 认领 / 释放事件均发出（s16 同源）。"""
    bus = EventBus()
    captured = []
    async def cap(e):
        captured.append(e)
    bus.subscribe("task_status_changed", cap)
    bus.subscribe("task_claimed", cap)
    bus.subscribe("task_released", cap)

    board = TaskBoard(event_bus=bus, session_id="s")
    await board.add("a", "x")
    await board.claim("w1")
    await board.release("a")

    types = [e.event_type for e in captured]
    assert types.count("task_status_changed") >= 3
    assert any(e.event_type == "task_claimed" for e in captured)
    assert any(e.event_type == "task_released" for e in captured)


async def test_swarm_autonomous_claims_and_merges(tmp_path):
    """Swarm 自主模式：子任务上板，通用 worker 认领执行，结果合并。"""
    base_auth = ActionAuthorizer(workspace_root=str(tmp_path), confirmation_enabled=True)
    base = SimpleNamespace(
        auth=base_auth,
        max_iterations=10, thrashing_threshold=3, max_thrashing_events=3,
        max_tokens_per_task=1000, _confirm=None, total_timeout_seconds=120,
    )
    llm = AsyncMock()
    # decompose (1) + merge (1)
    llm.chat.side_effect = [
        LLMResponse('[{"id":"1","description":"a","file_scope":"src/a.py"},'
                    '{"id":"2","description":"b","file_scope":"src/b.py"}]'),
        LLMResponse("merged output"),
    ]

    calls = {"n": 0}
    def factory(spec):
        p = MagicMock()
        async def _exec(**kw):
            calls["n"] += 1
            return _board_result()
        p.execute = AsyncMock(side_effect=_exec)
        p.mode = PlanningMode.REACT
        return p

    planner = SwarmPlanner(
        react_planner=base, roles=[RoleSpec(role="coder", file_scope=True, count=2)],
        planner_factory=factory, autonomous=True, autonomous_workers=2,
    )

    bus = EventBus()
    session = MagicMock()
    session.id = "sess"
    session.fork.return_value = MagicMock()

    result = await planner.execute(
        task="实现功能", context=SimpleNamespace(), tools=MagicMock(),
        llm=llm, sandbox=MagicMock(), session=session, event_bus=bus,
    )

    # 每个子任务被恰好一个 worker 认领执行（无重复），结果合并。
    assert calls["n"] == 2
    assert result.output == "merged output"
    assert len(result.contributors) == 2
    assert result.status is ResultStatus.SUCCESS
