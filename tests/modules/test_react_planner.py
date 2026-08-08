"""Tests for ReAct Planner."""
import pytest
from unittest.mock import AsyncMock, patch
from synapse.modules.planning.react import ReActPlanner, _looks_like_code_task, _select_tool_schemas
from synapse.core.session import Session
from synapse.core.events import EventBus
from synapse.protocols.llm import LLMResponse, Message
from synapse.protocols.tool import ToolResult, ToolCallMetadata
from synapse.protocols.retriever import Context
from synapse.core.exceptions import ProviderError


def test_code_task_detection_and_tool_schema_trim():
    assert _looks_like_code_task("Fix the bug in calculator.py and run pytest")
    assert not _looks_like_code_task("Say hello")
    schemas = [{"name": name} for name in ("read", "shell", "web_search", "browser")]
    assert [item["name"] for item in _select_tool_schemas(schemas, "Fix calculator.py")] == ["read", "shell"]


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
async def test_code_task_without_verification_is_partial():
    mock_llm = AsyncMock()
    mock_llm.chat.return_value = LLMResponse(
        content="I changed calculator.py.", tool_calls=[], stop_reason="end_turn",
        usage={"input": 10, "output": 5},
    )
    mock_tools = AsyncMock()
    mock_tools.get_schemas.return_value = [{"name": "shell", "description": "", "input_schema": {}}]
    result = await ReActPlanner().execute(
        task="Fix the bug in calculator.py",
        context=Context(), tools=mock_tools, llm=mock_llm, sandbox=AsyncMock(),
        session=Session(), event_bus=EventBus(),
    )
    assert result.status.value == "partial"
    assert "验证命令" in result.output


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
def _cancel_llm_fixtures():
    """Shared AsyncMock llm/tools for loop tests (stream() raises TypeError so
    the loop falls back to chat(), which is what we drive)."""
    mock_tool = AsyncMock()
    mock_tool.execute.return_value = ToolResult(
        success=True, output="ok",
        metadata=ToolCallMetadata(tool_name="read"),
    )
    mock_tools = AsyncMock()
    mock_tools.get.return_value = mock_tool
    mock_tools.get_schemas.return_value = [{"name": "read", "description": "Read", "input_schema": {}}]
    return mock_tool, mock_tools


@pytest.mark.asyncio
async def test_react_cancels_and_persists_progress():
    """A cancel requested mid-run stops the loop at the next boundary (PARTIAL)
    and writes the in-progress messages back so the session can be saved."""
    mock_tool, mock_tools = _cancel_llm_fixtures()
    mock_llm = AsyncMock()
    calls = {"n": 0}
    planner = ReActPlanner(max_iterations=50)

    def llm_side_effect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            planner.request_cancel()  # cancel during iteration 2
        return LLMResponse(
            content="",
            tool_calls=[{"id": "t1", "name": "read", "input": {"path": "/test.txt"}}],
            stop_reason="tool_use",
            usage={"input": 5, "output": 2},
        )

    mock_llm.chat.side_effect = llm_side_effect
    session = Session()

    result = await planner.execute(
        task="long running task",
        context=Context(),
        tools=mock_tools,
        llm=mock_llm,
        sandbox=AsyncMock(),
        session=session,
        event_bus=EventBus(),
    )

    assert result.status.value == "partial"
    assert "中断" in result.output
    # The loop did run, then stopped at the next boundary after the cancel.
    assert mock_llm.chat.call_count >= 1
    # Progress (the user task message) is written back for the caller to save.
    assert any(m.role == "user" for m in session.messages)


@pytest.mark.asyncio
async def test_react_stale_cancel_cleared():
    """A cancel flag left over from a previous run must not poison the next
    run on the same singleton planner (sticky-cancel fix)."""
    mock_tool, mock_tools = _cancel_llm_fixtures()
    mock_llm = AsyncMock()
    mock_llm.chat.side_effect = [
        LLMResponse(content="", tool_calls=[{"id": "t1", "name": "read", "input": {"path": "/a"}}],
                    stop_reason="tool_use", usage={"input": 5, "output": 2}),
        LLMResponse(content="", tool_calls=[{"id": "t2", "name": "read", "input": {"path": "/b"}}],
                    stop_reason="tool_use", usage={"input": 5, "output": 2}),
        LLMResponse(content="Done", tool_calls=[], stop_reason="end_turn", usage={"input": 5, "output": 2}),
    ]
    planner = ReActPlanner(max_iterations=50)
    session = Session()
    planner.request_cancel()  # stale flag from a hypothetical previous run

    result = await planner.execute(
        task="t", context=Context(), tools=mock_tools, llm=mock_llm,
        sandbox=AsyncMock(), session=session, event_bus=EventBus(),
    )

    assert result.status.value == "success"  # stale cancel ignored, run completes
    assert mock_llm.chat.call_count == 3


@pytest.mark.asyncio
async def test_react_malformed_tool_call_skipped():
    """A tool call missing 'name' (or with non-dict input) must not KeyError
    the whole task — it's filtered out and the valid one still runs."""
    mock_tool, mock_tools = _cancel_llm_fixtures()
    mock_llm = AsyncMock()
    mock_llm.chat.side_effect = [
        LLMResponse(content="", tool_calls=[
            {"id": "t1", "name": "read", "input": {"path": "/a"}},
            {"id": "t2", "input": "not a dict"},  # malformed: no name, str input
        ], stop_reason="tool_use", usage={"input": 5, "output": 2}),
        LLMResponse(content="Done", tool_calls=[], stop_reason="end_turn", usage={"input": 5, "output": 2}),
    ]
    planner = ReActPlanner(max_iterations=50)
    session = Session()
    result = await planner.execute(
        task="t", context=Context(), tools=mock_tools, llm=mock_llm,
        sandbox=AsyncMock(), session=session, event_bus=EventBus(),
    )
    assert result.status.value == "success"
    # t1 ran; the malformed t2 was skipped without crashing.
    assert mock_tool.execute.await_count >= 1


@pytest.mark.asyncio
async def test_react_llm_call_times_out(monkeypatch):
    """A hanging LLM call must be bounded by wait_for and fail the task after
    retries — previously it blocked the loop forever."""
    import asyncio as _asyncio

    async def _fast_sleep(delay):
        return None

    monkeypatch.setattr(_asyncio, "sleep", _fast_sleep)

    mock_tool, mock_tools = _cancel_llm_fixtures()
    mock_llm = AsyncMock()

    async def _hang(*args, **kwargs):
        # Never completes; wait_for must cancel it. Not sleep(), since the
        # monkeypatched fast backoff above would make it return immediately.
        await _asyncio.Event().wait()

    mock_llm.chat.side_effect = _hang

    planner = ReActPlanner(max_iterations=3, llm_timeout_seconds=0.05)
    session = Session()
    result = await planner.execute(
        task="t", context=Context(), tools=mock_tools, llm=mock_llm,
        sandbox=AsyncMock(), session=session, event_bus=EventBus(),
    )
    assert result.status.value == "failed"
    assert "LLM API call failed" in result.output


@pytest.mark.asyncio
async def test_react_auth_error_fails_without_retry(monkeypatch):
    """401/403 failures are deterministic and must not spend retry backoff."""
    import asyncio as _asyncio

    sleeps = []

    async def _record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(_asyncio, "sleep", _record_sleep)

    mock_llm = AsyncMock()
    mock_llm.stream.side_effect = TypeError
    mock_llm.chat.side_effect = ProviderError(
        "Anthropic streaming error: Error code: 401 authentication_error invalid api key"
    )
    mock_tools = AsyncMock()
    mock_tools.get_schemas.return_value = []

    planner = ReActPlanner(max_iterations=1, max_llm_retries=3)
    result = await planner.execute(
        task="test", context=Context(), tools=mock_tools, llm=mock_llm,
        sandbox=AsyncMock(), session=Session(), event_bus=EventBus(),
    )

    assert result.status.value == "failed"
    assert "after 1 attempt" in result.output
    assert "retries skipped" in result.output
    assert mock_llm.chat.call_count == 1
    assert sleeps == []


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


@pytest.mark.asyncio
async def test_readonly_tools_run_concurrently():
    """Multiple READ_ONLY calls in one turn overlap instead of running serially."""
    import asyncio
    import time
    from synapse.protocols.tool import RiskLevel

    mock_llm = AsyncMock()
    mock_llm.chat.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[
                {"id": "t1", "name": "read", "input": {"path": "/a.txt"}},
                {"id": "t2", "name": "read", "input": {"path": "/b.txt"}},
                {"id": "t3", "name": "read", "input": {"path": "/c.txt"}},
            ],
            stop_reason="tool_use",
            usage={"input": 10, "output": 5},
        ),
        LLMResponse(content="done", tool_calls=[], stop_reason="end_turn",
                    usage={"input": 5, "output": 2}),
    ]

    class SlowRead:
        name = "read"
        risk_level = RiskLevel.READ_ONLY

        async def execute(self, params, sandbox=None):
            await asyncio.sleep(0.3)
            return ToolResult(success=True, output="x",
                              metadata=ToolCallMetadata(tool_name="read"))

    mock_tools = AsyncMock()
    mock_tools.get.return_value = SlowRead()

    planner = ReActPlanner(max_iterations=5)
    t0 = time.perf_counter()
    result = await planner.execute(
        task="read three files", context=Context(), tools=mock_tools,
        llm=mock_llm, sandbox=AsyncMock(), session=Session(), event_bus=EventBus(),
    )
    elapsed = time.perf_counter() - t0

    assert result.metrics.tool_call_count == 3
    # Serial would be >=0.9s; concurrent should land near 0.3s.
    assert elapsed < 0.6, f"read-only tools did not overlap (took {elapsed:.2f}s)"


@pytest.mark.asyncio
async def test_write_tools_stay_serial():
    """WRITE_LOCAL calls must not be parallelized — ordering matters."""
    import asyncio
    from synapse.protocols.tool import RiskLevel

    mock_llm = AsyncMock()
    mock_llm.chat.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[
                {"id": "t1", "name": "write", "input": {"path": "/a.txt"}},
                {"id": "t2", "name": "write", "input": {"path": "/b.txt"}},
            ],
            stop_reason="tool_use",
            usage={"input": 10, "output": 5},
        ),
        LLMResponse(content="done", tool_calls=[], stop_reason="end_turn",
                    usage={"input": 5, "output": 2}),
    ]

    concurrent = 0
    peak = 0

    class SlowWrite:
        name = "write"
        risk_level = RiskLevel.WRITE_LOCAL

        async def execute(self, params, sandbox=None):
            nonlocal concurrent, peak
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0.05)
            concurrent -= 1
            return ToolResult(success=True, output="ok",
                              metadata=ToolCallMetadata(tool_name="write"))

    mock_tools = AsyncMock()
    mock_tools.get.return_value = SlowWrite()

    await planner_execute_writes(mock_llm, mock_tools)
    assert peak == 1, f"writes overlapped (peak concurrency {peak})"


async def planner_execute_writes(mock_llm, mock_tools):
    planner = ReActPlanner(max_iterations=5)
    return await planner.execute(
        task="write two files", context=Context(), tools=mock_tools,
        llm=mock_llm, sandbox=AsyncMock(), session=Session(), event_bus=EventBus(),
    )
