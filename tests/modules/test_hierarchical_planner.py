"""Tests for HierarchicalPlanner."""
import pytest
from unittest.mock import AsyncMock, Mock, call
from synapse.modules.planning.react import ReActPlanner
from synapse.core.session import Session
from synapse.core.events import EventBus
from synapse.protocols.llm import LLMResponse
from synapse.protocols.tool import ToolResult, ToolCallMetadata
from synapse.protocols.planner import AgentResult, ExecutionMetrics, ResultStatus
from synapse.protocols.retriever import Context
from synapse.protocols.events import TaskDecomposed, MergeResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent_result(output: str, status: ResultStatus = ResultStatus.SUCCESS) -> AgentResult:
    return AgentResult(status=status, output=output, metrics=ExecutionMetrics())


def _subtask_json(subtasks: list[dict]) -> str:
    import json
    return json.dumps(subtasks)


@pytest.mark.asyncio
async def test_decomposes_task():
    """LLM decomposes the task into subtasks; TaskDecomposed event is emitted."""
    subtasks = [
        {"id": "1", "description": "Analyze codebase", "complexity": 3},
        {"id": "2", "description": "Implement feature", "complexity": 8},
        {"id": "3", "description": "Write tests", "complexity": 4},
    ]

    mock_llm = AsyncMock()
    mock_llm.chat.side_effect = [
        LLMResponse(content=_subtask_json(subtasks), usage={"input": 20, "output": 10}),
        LLMResponse(content="Merged final output.", usage={"input": 15, "output": 5}),
    ]

    mock_react = AsyncMock(spec=ReActPlanner)
    mock_react.mode = "react"
    mock_react.execute.side_effect = [
        _make_agent_result("Analysis complete."),
        _make_agent_result("Feature implemented."),
        _make_agent_result("Tests written."),
    ]

    mock_tools = AsyncMock()
    mock_tools.get_schemas.return_value = []
    mock_sandbox = AsyncMock()
    event_bus = EventBus()

    # Collect events
    decomposed_events = []
    merge_events = []

    async def on_decomposed(event: TaskDecomposed):
        decomposed_events.append(event)

    async def on_merge(event: MergeResult):
        merge_events.append(event)

    event_bus.subscribe("task_decomposed", on_decomposed)
    event_bus.subscribe("merge_result", on_merge)

    # Must import after mock setup (the planner module is the SUT)
    from synapse.modules.planning.hierarchical import HierarchicalPlanner

    planner = HierarchicalPlanner(react_planner=mock_react)
    session = Session()

    result = await planner.execute(
        task="Build a new feature end-to-end",
        context=Context(),
        tools=mock_tools,
        llm=mock_llm,
        sandbox=mock_sandbox,
        session=session,
        event_bus=event_bus,
    )

    # TaskDecomposed event emitted
    assert len(decomposed_events) == 1
    ev = decomposed_events[0]
    assert ev.subtask_ids == ["1", "2", "3"]
    assert ev.subtask_count == 3
    assert ev.session_id == session.id

    # Each subtask was executed by the react planner
    assert mock_react.execute.call_count == 3

    # LLM was called for decomposition + merge
    assert mock_llm.chat.call_count == 2


@pytest.mark.asyncio
async def test_executes_subtasks_serially():
    """Each subtask gets its own forked session and executes in order."""
    subtasks = [
        {"id": "a", "description": "Step A", "complexity": 2},
        {"id": "b", "description": "Step B", "complexity": 5},
    ]

    mock_llm = AsyncMock()
    mock_llm.chat.side_effect = [
        LLMResponse(content=_subtask_json(subtasks), usage={"input": 10, "output": 5}),
        LLMResponse(content="Merged.", usage={"input": 8, "output": 3}),
    ]

    # Track execution order
    execution_order = []

    async def tracked_execute(task, context, tools, llm, sandbox, session, event_bus):
        execution_order.append(session.id)
        return _make_agent_result(f"Done: {task}")

    mock_react = AsyncMock(spec=ReActPlanner)
    mock_react.mode = "react"
    mock_react.execute.side_effect = tracked_execute

    mock_tools = AsyncMock()
    mock_tools.get_schemas.return_value = []
    mock_sandbox = AsyncMock()
    event_bus = EventBus()

    from synapse.modules.planning.hierarchical import HierarchicalPlanner

    planner = HierarchicalPlanner(react_planner=mock_react)
    session = Session()
    parent_id = session.id

    # Wrap fork to track child sessions
    child_sessions = []
    original_fork = session.fork

    def tracking_fork(new_id):
        child = original_fork(new_id)
        child_sessions.append(child)
        return child

    session.fork = tracking_fork

    await planner.execute(
        task="Do A then B",
        context=Context(),
        tools=mock_tools,
        llm=mock_llm,
        sandbox=mock_sandbox,
        session=session,
        event_bus=event_bus,
    )

    # Two child sessions were created
    assert len(child_sessions) == 2
    assert child_sessions[0].id == "a"
    assert child_sessions[1].id == "b"

    # The child sessions were passed to the sub-planner (not the parent)
    assert execution_order == ["a", "b"]

    # Each child inherited parent's messages
    assert child_sessions[0].id != parent_id
    assert child_sessions[1].id != parent_id


@pytest.mark.asyncio
async def test_merges_results():
    """Final output merges all subtask results; MergeResult event emitted."""
    subtasks = [
        {"id": "1", "description": "Read files", "complexity": 2},
        {"id": "2", "description": "Write report", "complexity": 3},
    ]

    merged_output = "SUMMARY: Read 3 files and generated report.md covering all findings."

    mock_llm = AsyncMock()
    mock_llm.chat.side_effect = [
        LLMResponse(content=_subtask_json(subtasks), usage={"input": 10, "output": 5}),
        LLMResponse(content=merged_output, usage={"input": 20, "output": 10}),
    ]

    mock_react = AsyncMock(spec=ReActPlanner)
    mock_react.mode = "react"
    mock_react.execute.side_effect = [
        _make_agent_result("Files: a.txt, b.txt, c.txt"),
        _make_agent_result("Report: report.md created."),
    ]

    mock_tools = AsyncMock()
    mock_tools.get_schemas.return_value = []
    mock_sandbox = AsyncMock()
    event_bus = EventBus()

    merge_events = []

    async def on_merge(event: MergeResult):
        merge_events.append(event)

    event_bus.subscribe("merge_result", on_merge)

    from synapse.modules.planning.hierarchical import HierarchicalPlanner

    planner = HierarchicalPlanner(react_planner=mock_react)
    session = Session()

    result = await planner.execute(
        task="Read files and write report",
        context=Context(),
        tools=mock_tools,
        llm=mock_llm,
        sandbox=mock_sandbox,
        session=session,
        event_bus=event_bus,
    )

    # Final output is the merged content
    assert result.output == merged_output
    assert result.status == ResultStatus.SUCCESS

    # MergeResult event emitted with correct data
    assert len(merge_events) == 1
    ev = merge_events[0]
    assert ev.subtask_count == 2
    assert ev.merged_output == merged_output
    assert ev.session_id == session.id

    # LLM was called for merge (2nd call received subtask results)
    merge_call_args = mock_llm.chat.call_args_list[1]
    merge_messages = merge_call_args[0][0]  # first positional arg = messages
    merge_user_content = merge_messages[-1].content  # last message = user
    assert "Files: a.txt, b.txt, c.txt" in merge_user_content
    assert "Report: report.md created." in merge_user_content
