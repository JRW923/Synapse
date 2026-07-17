"""Tests for PlanExecutePlanner."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, call
from synapse.modules.planning.plan_execute import PlanExecutePlanner
from synapse.modules.planning.react import ReActPlanner
from synapse.core.session import Session
from synapse.core.events import EventBus
from synapse.protocols.llm import LLMResponse, Message
from synapse.protocols.tool import ToolResult, ToolCallMetadata
from synapse.protocols.retriever import Context
from synapse.protocols.planner import AgentResult, ResultStatus, ExecutionMetrics
from synapse.protocols.events import PlanCreated


PLAN_JSON = json.dumps({
    "reasoning": "We need to read the file, then write the output.",
    "steps": [
        {"step_id": "1", "description": "Read the input file", "expected_tools": ["read"]},
        {"step_id": "2", "description": "Process the data", "expected_tools": ["shell"]},
        {"step_id": "3", "description": "Write the output file", "expected_tools": ["write"]},
    ]
})


@pytest.mark.asyncio
async def test_generates_plan():
    """Phase 1: LLM returns a plan JSON, and it is parsed into steps."""
    # Mock LLM that returns a plan JSON
    mock_llm = AsyncMock()
    mock_llm.chat.return_value = LLMResponse(
        content=PLAN_JSON,
        tool_calls=[],
        stop_reason="end_turn",
        usage={"input": 50, "output": 30},
    )

    # Mock ReActPlanner — it should NOT be called during plan generation
    mock_react = AsyncMock(spec=ReActPlanner)
    mock_react.mode = ReActPlanner.mode

    mock_tools = AsyncMock()
    mock_sandbox = AsyncMock()
    event_bus = EventBus()

    planner = PlanExecutePlanner(react_planner=mock_react)
    session = Session()
    context = Context()

    # Capture PlanCreated events
    plan_events = []

    async def on_plan(event):
        plan_events.append(event)

    event_bus.subscribe("plan_created", on_plan)

    result = await planner.execute(
        task="Read, process, and write a file",
        context=context,
        tools=mock_tools,
        llm=mock_llm,
        sandbox=mock_sandbox,
        session=session,
        event_bus=event_bus,
    )

    # The plan was parsed: 3 steps
    assert len(plan_events) == 1
    assert isinstance(plan_events[0], PlanCreated)
    assert len(plan_events[0].plan_steps) == 3
    assert plan_events[0].plan_steps[0]["step_id"] == "1"
    assert plan_events[0].plan_steps[1]["step_id"] == "2"
    assert plan_events[0].plan_steps[2]["step_id"] == "3"
    assert plan_events[0].plan_steps[0]["description"] == "Read the input file"


@pytest.mark.asyncio
async def test_executes_all_steps():
    """Phase 2: Each plan step is delegated to ReActPlanner.execute()."""
    mock_llm = AsyncMock()
    mock_llm.chat.return_value = LLMResponse(
        content=PLAN_JSON,
        tool_calls=[],
        stop_reason="end_turn",
        usage={"input": 50, "output": 30},
    )

    # Mock ReActPlanner: return success for each step
    mock_react = AsyncMock(spec=ReActPlanner)
    mock_react.mode = ReActPlanner.mode
    mock_react.execute.side_effect = [
        AgentResult(status=ResultStatus.SUCCESS, output="Step 1 done",
                     metrics=ExecutionMetrics(tool_call_count=1)),
        AgentResult(status=ResultStatus.SUCCESS, output="Step 2 done",
                     metrics=ExecutionMetrics(tool_call_count=2)),
        AgentResult(status=ResultStatus.SUCCESS, output="Step 3 done",
                     metrics=ExecutionMetrics(tool_call_count=1)),
    ]

    mock_tools = AsyncMock()
    mock_sandbox = AsyncMock()
    event_bus = EventBus()

    planner = PlanExecutePlanner(react_planner=mock_react)
    session = Session()
    context = Context()

    result = await planner.execute(
        task="Read, process, and write a file",
        context=context,
        tools=mock_tools,
        llm=mock_llm,
        sandbox=mock_sandbox,
        session=session,
        event_bus=event_bus,
    )

    # ReActPlanner.execute() called once per step
    assert mock_react.execute.call_count == 3

    # Verify each call was for the right step description
    call_args_list = mock_react.execute.call_args_list
    assert call_args_list[0].kwargs["task"] == "Read the input file"
    assert call_args_list[1].kwargs["task"] == "Process the data"
    assert call_args_list[2].kwargs["task"] == "Write the output file"

    # Result aggregates metrics across all steps
    assert result.status == ResultStatus.SUCCESS
    assert result.metrics.tool_call_count == 4  # 1 + 2 + 1


@pytest.mark.asyncio
async def test_verification_detects_skipped_steps():
    """Phase 3: If a step fails or is skipped, verification reports it."""
    # Plan has 3 steps
    plan_with_extra = json.dumps({
        "reasoning": "Task with three steps.",
        "steps": [
            {"step_id": "1", "description": "Step one", "expected_tools": ["read"]},
            {"step_id": "2", "description": "Step two", "expected_tools": ["shell"]},
            {"step_id": "3", "description": "Step three", "expected_tools": ["write"]},
        ]
    })

    mock_llm = AsyncMock()
    mock_llm.chat.return_value = LLMResponse(
        content=plan_with_extra,
        tool_calls=[],
        stop_reason="end_turn",
        usage={"input": 50, "output": 30},
    )

    # Step 2 fails — this means it's effectively skipped
    mock_react = AsyncMock(spec=ReActPlanner)
    mock_react.mode = ReActPlanner.mode
    mock_react.execute.side_effect = [
        AgentResult(status=ResultStatus.SUCCESS, output="Step one done",
                     metrics=ExecutionMetrics()),
        AgentResult(status=ResultStatus.FAILED, output="Step two failed",
                     metrics=ExecutionMetrics()),
        AgentResult(status=ResultStatus.SUCCESS, output="Step three done",
                     metrics=ExecutionMetrics()),
    ]

    mock_tools = AsyncMock()
    mock_sandbox = AsyncMock()
    event_bus = EventBus()

    planner = PlanExecutePlanner(react_planner=mock_react)
    session = Session()
    context = Context()

    result = await planner.execute(
        task="Three-step task",
        context=context,
        tools=mock_tools,
        llm=mock_llm,
        sandbox=mock_sandbox,
        session=session,
        event_bus=event_bus,
    )

    # The overall result should be PARTIAL because step 2 failed
    assert result.status == ResultStatus.PARTIAL

    # The output should mention the failed/skipped step
    assert "Step two" in result.output or "step_id" in result.output \
        or "2" in result.output or "failed" in result.output.lower()
