"""Integration test -- full pipeline with mock LLM."""
import pytest
from unittest.mock import AsyncMock, patch
from synapse.core.container import Container
from synapse.core.agent import Agent
from synapse.core.session import Session
from synapse.core.events import EventBus
from synapse.modules.providers.anthropic import AnthropicProvider
from synapse.modules.planning.react import ReActPlanner
from synapse.modules.tools.registry import DefaultToolRegistry
from synapse.modules.tools.file_read import ReadTool
from synapse.modules.tools.file_write import WriteTool
from synapse.modules.tools.file_edit import EditTool
from synapse.modules.tools.file_glob import GlobTool
from synapse.modules.tools.search_grep import GrepTool
from synapse.modules.tools.shell import ShellTool
from synapse.modules.tools.git_ import GitTool
from synapse.modules.memory.session import SessionMemory
from synapse.modules.context.retriever import BasicContextRetriever
from synapse.modules.security.sandbox import ProcessSandbox
from synapse.protocols.llm import LLMProvider
from synapse.protocols.planner import Planner
from synapse.protocols.tool import ToolRegistry
from synapse.protocols.memory import MemoryStore
from synapse.protocols.retriever import ContextRetriever
from synapse.protocols.sandbox import Sandbox
from synapse.protocols.llm import LLMResponse


def build_test_container():
    """Build container with real modules but a mock LLM."""
    c = Container()
    c.register(EventBus, EventBus())

    mock_llm = AsyncMock()
    mock_llm.model_id = "mock"
    mock_llm.chat.return_value = LLMResponse(
        content="Task completed successfully.",
        tool_calls=[],
        stop_reason="end_turn",
        usage={"input": 10, "output": 5},
    )
    c.register(LLMProvider, mock_llm)

    registry = DefaultToolRegistry()
    registry.register(ReadTool())
    registry.register(WriteTool())
    registry.register(EditTool())
    registry.register(GlobTool())
    registry.register(GrepTool())
    registry.register(ShellTool())
    registry.register(GitTool())
    c.register(ToolRegistry, registry)

    c.register(MemoryStore, SessionMemory())
    c.register(ContextRetriever, BasicContextRetriever())
    c.register(Sandbox, ProcessSandbox())
    c.register(Planner, ReActPlanner(max_iterations=50))

    return c


@pytest.mark.asyncio
async def test_full_pipeline_no_tools():
    """Agent completes a task that requires no tool calls."""
    c = build_test_container()
    agent = Agent(c)
    session = Session()

    result = await agent.run("Say hello", session)

    assert result.status.value == "success"
    assert "Task completed" in result.output


@pytest.mark.asyncio
async def test_full_pipeline_with_writes(tmp_path):
    """Agent writes and reads a file."""
    c = build_test_container()

    # Override mock LLM to make a write call then a read call then finish
    mock_llm = c.resolve(LLMProvider)
    mock_llm.chat.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[{"id": "w1", "name": "write", "input": {
                "path": str(tmp_path / "output.txt"), "content": "hello from synapse"
            }}],
            stop_reason="tool_use",
            usage={"input": 15, "output": 10},
        ),
        LLMResponse(
            content="",
            tool_calls=[{"id": "r1", "name": "read", "input": {"path": str(tmp_path / "output.txt")}}],
            stop_reason="tool_use",
            usage={"input": 5, "output": 3},
        ),
        LLMResponse(
            content="I have written and verified the file.",
            tool_calls=[],
            stop_reason="end_turn",
            usage={"input": 8, "output": 5},
        ),
    ]

    agent = Agent(c)
    session = Session()

    result = await agent.run("Write a file", session)

    assert result.status.value == "success"
    assert result.metrics.tool_call_count == 2
    # Verify file was actually written
    assert (tmp_path / "output.txt").read_text() == "hello from synapse"
