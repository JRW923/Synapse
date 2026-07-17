"""Tests for ReAct Planner."""
import pytest
from unittest.mock import AsyncMock, patch
from synapse.modules.planning.react import ReActPlanner
from synapse.core.session import Session
from synapse.core.events import EventBus
from synapse.protocols.llm import LLMResponse, Message
from synapse.protocols.tool import ToolResult, ToolCallMetadata
from synapse.protocols.retriever import Context


@pytest.mark.asyncio
async def test_react_completes_without_tool_calls():
    """If LLM returns text without tool calls, loop ends immediately."""
    mock_llm = AsyncMock()
    mock_llm.chat.return_value = LLMResponse(
        content="I have completed the task.",
        tool_calls=[],
        stop_reason="end_turn",
        usage={"input": 10, "output": 5},
    )

    mock_tools = AsyncMock()
    mock_sandbox = AsyncMock()
    event_bus = EventBus()

    planner = ReActPlanner(max_iterations=50)
    session = Session()
    context = Context()

    result = await planner.execute(
        task="Say hello",
        context=context,
        tools=mock_tools,
        llm=mock_llm,
        sandbox=mock_sandbox,
        session=session,
        event_bus=event_bus,
    )

    assert result.status.value == "success"
    assert "I have completed" in result.output
    assert result.metrics.tool_call_count == 0


@pytest.mark.asyncio
async def test_react_calls_tool():
    """LLM requests tool → tool executes → result fed back to LLM."""
    mock_llm = AsyncMock()
    # First call: tool_use
    mock_llm.chat.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[{"id": "t1", "name": "read", "input": {"path": "/test.txt"}}],
            stop_reason="tool_use",
            usage={"input": 10, "output": 5},
        ),
        # Second call: final text response
        LLMResponse(
            content="File contents: hello",
            tool_calls=[],
            stop_reason="end_turn",
            usage={"input": 15, "output": 8},
        ),
    ]

    mock_tool = AsyncMock()
    mock_tool.execute.return_value = ToolResult(
        success=True, output="hello",
        metadata=ToolCallMetadata(tool_name="read"),
    )
    mock_tools = AsyncMock()
    mock_tools.get.return_value = mock_tool

    mock_sandbox = AsyncMock()
    event_bus = EventBus()

    planner = ReActPlanner(max_iterations=50)
    session = Session()

    result = await planner.execute(
        task="Read test.txt",
        context=Context(),
        tools=mock_tools,
        llm=mock_llm,
        sandbox=mock_sandbox,
        session=session,
        event_bus=event_bus,
    )

    assert result.metrics.tool_call_count == 1
    assert mock_tool.execute.called


@pytest.mark.asyncio
async def test_react_hits_max_iterations():
    """When loop exceeds max_iterations, return PARTIAL."""
    mock_llm = AsyncMock()
    mock_llm.chat.return_value = LLMResponse(
        content="",
        tool_calls=[{"id": "t1", "name": "read", "input": {"path": "/test.txt"}}],
        stop_reason="tool_use",
        usage={"input": 5, "output": 2},
    )

    mock_tool = AsyncMock()
    mock_tool.execute.return_value = ToolResult(
        success=True, output="ok",
        metadata=ToolCallMetadata(tool_name="read"),
    )
    mock_tools = AsyncMock()
    mock_tools.get.return_value = mock_tool
    mock_tools.get_schemas.return_value = [{"name": "read", "description": "Read", "input_schema": {}}]

    mock_sandbox = AsyncMock()
    event_bus = EventBus()

    planner = ReActPlanner(max_iterations=3)
    session = Session()

    result = await planner.execute(
        task="test",
        context=Context(),
        tools=mock_tools,
        llm=mock_llm,
        sandbox=mock_sandbox,
        session=session,
        event_bus=event_bus,
    )

    assert result.status.value == "partial"
    assert "Max iterations" in result.output


@pytest.mark.asyncio
async def test_react_detects_thrashing():
    """Same file modified > threshold → emit event."""
    mock_llm = AsyncMock()
    mock_llm.chat.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[{"id": f"t{i}", "name": "write", "input": {"path": "/same_file.py"}}],
            stop_reason="tool_use",
            usage={"input": 5, "output": 2},
        )
        for i in range(5)
    ] + [
        LLMResponse(
            content="Done",
            tool_calls=[],
            stop_reason="end_turn",
            usage={"input": 5, "output": 2},
        ),
    ]

    mock_tool = AsyncMock()
    mock_tool.execute.return_value = ToolResult(
        success=True, output="ok",
        metadata=ToolCallMetadata(tool_name="write", files_touched=["/same_file.py"]),
    )
    mock_tools = AsyncMock()
    mock_tools.get.return_value = mock_tool
    mock_tools.get_schemas.return_value = []

    mock_sandbox = AsyncMock()
    event_bus = EventBus()
    thrashing_events = []

    async def on_thrashing(event):
        thrashing_events.append(event)

    event_bus.subscribe("thrashing_detected", on_thrashing)

    planner = ReActPlanner(max_iterations=10, thrashing_threshold=3)
    session = Session()

    await planner.execute(
        task="test",
        context=Context(),
        tools=mock_tools,
        llm=mock_llm,
        sandbox=mock_sandbox,
        session=session,
        event_bus=event_bus,
    )

    assert len(thrashing_events) > 0
