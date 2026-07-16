"""Tests for EventBus."""
import asyncio
import pytest
from synapse.core.events import EventBus
from synapse.protocols.events import ToolCallStarted


@pytest.mark.asyncio
async def test_subscribe_and_emit():
    bus = EventBus()
    received: list[ToolCallStarted] = []

    async def handler(event: ToolCallStarted):
        received.append(event)

    bus.subscribe("tool_call_started", handler)
    event = ToolCallStarted(session_id="s1", tool_name="read", tool_params={})
    await bus.emit(event)

    assert len(received) == 1
    assert received[0].tool_name == "read"
    assert received[0].session_id == "s1"


@pytest.mark.asyncio
async def test_unsubscribe():
    bus = EventBus()
    received: list = []

    async def handler(event):
        received.append(event)

    bus.subscribe("tool_call_started", handler)
    bus.unsubscribe("tool_call_started", handler)
    await bus.emit(ToolCallStarted(session_id="s1", tool_name="read", tool_params={}))

    assert len(received) == 0


@pytest.mark.asyncio
async def test_multiple_handlers():
    bus = EventBus()
    results: list[str] = []

    async def h1(event):
        results.append("h1")

    async def h2(event):
        results.append("h2")

    bus.subscribe("tool_call_started", h1)
    bus.subscribe("tool_call_started", h2)
    await bus.emit(ToolCallStarted(session_id="s1", tool_name="read", tool_params={}))

    assert results == ["h1", "h2"]


@pytest.mark.asyncio
async def test_handler_exception_does_not_block_others():
    bus = EventBus()
    results: list[str] = []

    async def h_bad(event):
        raise RuntimeError("boom")

    async def h_good(event):
        results.append("good")

    bus.subscribe("tool_call_started", h_bad)
    bus.subscribe("tool_call_started", h_good)
    # Should not raise — h_good should still fire
    await bus.emit(ToolCallStarted(session_id="s1", tool_name="read", tool_params={}))

    assert results == ["good"]
