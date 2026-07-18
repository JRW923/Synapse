"""Phase 2 integration tests — full pipeline with Phase 2 modules."""
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from synapse.core.container import Container
from synapse.core.agent import Agent
from synapse.core.session import Session
from synapse.core.events import EventBus

from synapse.protocols.llm import LLMProvider, LLMResponse, Message
from synapse.protocols.planner import Planner, AgentResult, PlanningMode, ResultStatus, ExecutionMetrics
from synapse.protocols.tool import ToolRegistry
from synapse.protocols.memory import MemoryStore, MemoryEntry, MemoryLevel, MemoryMetadata
from synapse.protocols.retriever import ContextRetriever
from synapse.protocols.sandbox import Sandbox
from synapse.protocols.events import (
    ToolCallStarted,
    ToolCallCompleted,
    AuthDecisionMade,
    AgentCompleted,
)

from synapse.modules.tools.registry import DefaultToolRegistry
from synapse.modules.tools.file_read import ReadTool
from synapse.modules.tools.file_write import WriteTool
from synapse.modules.tools.file_edit import EditTool
from synapse.modules.tools.file_glob import GlobTool
from synapse.modules.tools.search_grep import GrepTool
from synapse.modules.tools.shell import ShellTool
from synapse.modules.tools.git_ import GitTool
from synapse.modules.memory.session import SessionMemory
from synapse.modules.memory.project import ProjectMemory
from synapse.modules.memory.user import UserMemory
from synapse.modules.context.retriever import BasicContextRetriever
from synapse.modules.security.sandbox import ProcessSandbox
from synapse.modules.security.auth import ActionAuthorizer
from synapse.modules.security.audit import AuditLogger
from synapse.modules.planning.react import ReActPlanner
from synapse.modules.planning.plan_execute import PlanExecutePlanner


# ============================================================================
# Test 1: OpenAI provider in full pipeline via Synapse facade
# ============================================================================

PLAN_JSON_2_STEPS = json.dumps({
    "reasoning": "Two-step task: read then write.",
    "steps": [
        {"step_id": "1", "description": "Read the input file", "expected_tools": ["read"]},
        {"step_id": "2", "description": "Write the output file", "expected_tools": ["write"]},
    ],
})


@pytest.mark.asyncio
async def test_openai_provider_in_pipeline():
    """Use Synapse facade with openai provider (mock LLM) and verify agent.run()."""
    from synapse.adapters.library import Synapse

    # Build a mock LLM instance
    mock_llm = AsyncMock()
    mock_llm.model_id = "mock-gpt-4o"
    mock_llm.chat.return_value = LLMResponse(
        content="Task completed successfully via OpenAI.",
        tool_calls=[],
        stop_reason="end_turn",
        usage={"input": 10, "output": 5},
    )

    # Mock provider class that returns the mock instance on construction
    MockProvider = MagicMock(return_value=mock_llm)

    with patch("synapse.modules.providers.openai.OpenAIProvider", MockProvider):
        agent = Synapse(provider="openai", model="gpt-4o")
        result = await agent.run("Say hello from OpenAI")

    assert result.status == ResultStatus.SUCCESS
    assert "Task completed" in result.output
    assert "OpenAI" in result.output
    # Verify the LLM was called
    mock_llm.chat.assert_called()


# ============================================================================
# Test 2: Plan-Execute pipeline with mock LLM
# ============================================================================

def _make_step_result(status=ResultStatus.SUCCESS, output="Done", tool_calls=0):
    return AgentResult(status=status, output=output, metrics=ExecutionMetrics(tool_call_count=tool_calls))


@pytest.mark.asyncio
async def test_plan_execute_pipeline():
    """PlanExecute mode with mock LLM — returns plan JSON then step results."""
    # Mock LLM that returns:
    #   1st call: plan JSON (planning phase)
    #   2nd call: step 1 result
    #   3rd call: step 2 result
    mock_llm = AsyncMock()
    mock_llm.model_id = "mock-claude"
    mock_llm.chat.side_effect = [
        # Phase 1: plan generation
        LLMResponse(
            content=PLAN_JSON_2_STEPS,
            tool_calls=[],
            stop_reason="end_turn",
            usage={"input": 50, "output": 30},
        ),
        # Phase 2: step 1 execution (ReAct loop — single call each)
        LLMResponse(
            content="Step 1 completed: file read successfully.",
            tool_calls=[],
            stop_reason="end_turn",
            usage={"input": 20, "output": 15},
        ),
        # Phase 2: step 2 execution
        LLMResponse(
            content="Step 2 completed: file written successfully.",
            tool_calls=[],
            stop_reason="end_turn",
            usage={"input": 20, "output": 15},
        ),
    ]

    # Build a real container with PlanExecutePlanner
    c = Container()
    event_bus = EventBus()
    c.register(EventBus, event_bus)
    c.register(LLMProvider, mock_llm)

    registry = DefaultToolRegistry()
    registry.register(ReadTool())
    registry.register(WriteTool())
    c.register(ToolRegistry, registry)

    c.register(MemoryStore, SessionMemory())
    c.register(ContextRetriever, BasicContextRetriever())
    c.register(Sandbox, ProcessSandbox())

    auth = ActionAuthorizer(workspace_root=".", confirmation_enabled=False)
    c.register(ActionAuthorizer, auth)

    react = ReActPlanner(max_iterations=50, thrashing_threshold=3, auth=auth)
    planner = PlanExecutePlanner(react_planner=react)
    c.register(Planner, planner)

    audit = AuditLogger(bus=event_bus)
    c.register(AuditLogger, audit)

    agent = Agent(c)
    session = Session()

    result = await agent.run("Read input, then write output", session)

    # All steps succeeded => overall success
    assert result.status == ResultStatus.SUCCESS
    # Output should mention both steps
    assert "Step 1" in result.output or "step 1" in result.output.lower()
    assert "Step 2" in result.output or "step 2" in result.output.lower()
    # LLM called 3 times (1 plan + 2 steps)
    assert mock_llm.chat.call_count == 3
    # Metrics aggregated from both steps
    assert result.metrics.tokens_input == 90  # 50 + 20 + 20
    assert result.metrics.tokens_output == 60  # 30 + 15 + 15


# ============================================================================
# Test 3: Project memory pipeline — write then read across tasks
# ============================================================================

@pytest.mark.asyncio
async def test_project_memory_pipeline(tmp_path):
    """Task A writes to project memory; Task B reads it back via the same store."""
    memory_base = tmp_path / ".synapse" / "memory"
    project_memory = ProjectMemory(base_path=memory_base)

    # Create a layered memory store (same structure as Synapse facade)
    session_mem = SessionMemory()
    user_mem = UserMemory()

    # Build a simple LayeredMemory-like wrapper inline
    class TestLayeredMemory:
        def __init__(self, session_store, project_store, user_store):
            self._session = session_store
            self._project = project_store
            self._user = user_store

        async def store(self, entry: MemoryEntry) -> None:
            if entry.level == MemoryLevel.SESSION:
                await self._session.store(entry)
            elif entry.level == MemoryLevel.PROJECT:
                await self._project.store(entry)
            elif entry.level == MemoryLevel.USER:
                await self._user.store(entry)

        async def retrieve(self, query: str, level: MemoryLevel, top_k: int = 5):
            if level == MemoryLevel.SESSION:
                return await self._session.retrieve(query, level, top_k)
            if level == MemoryLevel.PROJECT:
                return await self._project.retrieve(query, level, top_k)
            if level == MemoryLevel.USER:
                return await self._user.retrieve(query, level, top_k)
            return []

        async def forget(self, entry_id: str) -> None:
            await self._session.forget(entry_id)
            await self._project.forget(entry_id)
            await self._user.forget(entry_id)

    layered_memory = TestLayeredMemory(session_mem, project_memory, user_mem)

    # ---- Helper to build a container + agent ----

    def _build_agent(llm_side_effect=None):
        mock_llm = AsyncMock()
        mock_llm.model_id = "mock"
        if llm_side_effect:
            mock_llm.chat.side_effect = llm_side_effect
        else:
            mock_llm.chat.return_value = LLMResponse(
                content="Done.", tool_calls=[], stop_reason="end_turn",
                usage={"input": 5, "output": 3},
            )

        c = Container()
        c.register(EventBus, EventBus())
        c.register(LLMProvider, mock_llm)
        registry = DefaultToolRegistry()
        registry.register(ReadTool())
        registry.register(WriteTool())
        c.register(ToolRegistry, registry)
        c.register(MemoryStore, layered_memory)
        c.register(ContextRetriever, BasicContextRetriever())
        c.register(Sandbox, ProcessSandbox())
        auth = ActionAuthorizer(workspace_root=str(tmp_path), confirmation_enabled=False)
        c.register(ActionAuthorizer, auth)
        c.register(Planner, ReActPlanner(max_iterations=50, thrashing_threshold=3, auth=auth))
        return Agent(c), mock_llm

    # ---- Task A: store a project-level memory entry ----

    # The Agent's _persist_memory writes SESSION-level entries automatically.
    # We also explicitly write a PROJECT-level entry that represents knowledge
    # we want to persist across sessions.

    project_entry = MemoryEntry(
        id="proj-arch-1",
        content="The codebase uses a hexagonal architecture with ports and adapters.",
        level=MemoryLevel.PROJECT,
        metadata=MemoryMetadata(tags=["architecture", "design"], priority=9),
    )
    await project_memory.store(project_entry)

    # Run Task A — the agent's _persist_memory also writes a SESSION entry
    agent_a, llm_a = _build_agent()
    result_a = await agent_a.run("Analyze the architecture", Session())
    assert result_a.status == ResultStatus.SUCCESS

    # ---- Task B: retrieve project memory ----

    # Simulate a new task that reads back the project-level knowledge
    agent_b, llm_b = _build_agent()
    result_b = await agent_b.run("How is the codebase architected?", Session())
    assert result_b.status == ResultStatus.SUCCESS

    # ---- Verify project memory persisted across tasks ----
    retrieved = await project_memory.retrieve("hexagonal architecture", MemoryLevel.PROJECT)
    assert len(retrieved) >= 1
    assert any("hexagonal" in e.content.lower() for e in retrieved)
    assert retrieved[0].metadata.priority == 9


# ============================================================================
# Test 4: Audit log events — run task, verify log file
# ============================================================================

@pytest.mark.asyncio
async def test_audit_log_events(tmp_path):
    """Run a task via the Agent pipeline; verify audit log file was created and
    contains entries for the events emitted during execution."""
    audit_dir = tmp_path / ".synapse" / "audit"

    # Mock LLM that makes a tool call then finishes
    mock_llm = AsyncMock()
    mock_llm.model_id = "mock"
    mock_llm.chat.side_effect = [
        # First turn: request a read
        LLMResponse(
            content="",
            tool_calls=[{
                "id": "r1", "name": "read",
                "input": {"path": str(tmp_path / "nonexistent.txt")},
            }],
            stop_reason="tool_use",
            usage={"input": 15, "output": 10},
        ),
        # Second turn: done
        LLMResponse(
            content="I attempted to read the file.",
            tool_calls=[],
            stop_reason="end_turn",
            usage={"input": 8, "output": 5},
        ),
    ]

    event_bus = EventBus()
    audit_logger = AuditLogger(bus=event_bus, audit_dir=str(audit_dir))

    c = Container()
    c.register(EventBus, event_bus)
    c.register(LLMProvider, mock_llm)

    registry = DefaultToolRegistry()
    registry.register(ReadTool())
    registry.register(WriteTool())
    c.register(ToolRegistry, registry)

    c.register(MemoryStore, SessionMemory())
    c.register(ContextRetriever, BasicContextRetriever())
    c.register(Sandbox, ProcessSandbox())

    auth = ActionAuthorizer(workspace_root=str(tmp_path), confirmation_enabled=False)
    c.register(ActionAuthorizer, auth)

    c.register(Planner, ReActPlanner(max_iterations=50, thrashing_threshold=3, auth=auth))
    c.register(AuditLogger, audit_logger)

    agent = Agent(c)
    session = Session()

    result = await agent.run("Read a file", session)

    assert result.status == ResultStatus.SUCCESS

    # Verify audit log file exists and contains entries
    audit_files = list(audit_dir.glob("*.jsonl"))
    assert len(audit_files) == 1, f"Expected 1 audit log file, found {len(audit_files)}"

    lines = audit_files[0].read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) >= 1, "Expected at least 1 audit log entry"

    # Each line must be valid JSON with a signature
    for line in lines:
        entry = json.loads(line)
        assert "signature" in entry, "Audit entry missing signature"
        assert len(entry["signature"]) == 64, "Signature must be 64-char hex digest"
        assert "event_type" in entry
        assert "session_id" in entry
        assert entry["session_id"] == session.id

    # Check that the audit log contains at least tool_call events
    event_types = [json.loads(line)["event_type"] for line in lines]
    has_tool_event = any(
        t in ("tool_call_started", "tool_call_completed", "auth_decision")
        for t in event_types
    )
    assert has_tool_event, f"Expected tool/auth events in audit log, got: {event_types}"

    # Verify HMAC signatures are valid
    for line in lines:
        entry = json.loads(line)
        assert audit_logger._verify_entry(entry), f"Invalid HMAC signature in entry: {entry.get('event_id')}"
