"""Tests for s16 团队协同协议完善.

验收：AgentMessage / TaskClaimed / TaskReleased 事件 schema 正确，且发送/订阅
往返拿到 payload。与 s17 TaskBoard 事件同源。
"""

from __future__ import annotations

from synapse.core.events import EventBus
from synapse.protocols.events import AgentMessage, TaskClaimed, TaskReleased


async def test_agent_message_round_trip():
    bus = EventBus()
    got = []
    async def cap(e):
        got.append(e)
    bus.subscribe("agent_message", cap)
    await bus.emit(AgentMessage(
        session_id="s", agent_id="a1", recipient="a2",
        message="需要 src/x.py 的合并结果", kind="request",
    ))
    assert len(got) == 1
    assert got[0].recipient == "a2"
    assert got[0].message == "需要 src/x.py 的合并结果"
    assert got[0].kind == "request"


async def test_task_claimed_released_round_trip():
    bus = EventBus()
    claimed, released = [], []
    async def on_claim(e):
        claimed.append(e)
    async def on_release(e):
        released.append(e)
    bus.subscribe("task_claimed", on_claim)
    bus.subscribe("task_released", on_release)

    await bus.emit(TaskClaimed(session_id="s", agent_id="w1", task_id="t1", owner="w1"))
    await bus.emit(TaskReleased(session_id="s", agent_id="w1", task_id="t1", owner="w1"))

    assert claimed and claimed[0].task_id == "t1" and claimed[0].owner == "w1"
    assert released and released[0].task_id == "t1"


async def test_broadcast_message_has_empty_recipient():
    msg = AgentMessage(session_id="s", agent_id="a1", message="开始评审", kind="notify")
    assert msg.recipient == ""
    assert msg.kind == "notify"
