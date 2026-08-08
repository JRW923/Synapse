"""Tests for the core Agent."""
import pytest
from unittest.mock import AsyncMock
from synapse.core.container import Container
from synapse.core.agent import Agent
from synapse.core.session import Session
from synapse.core.events import EventBus
from synapse.protocols.llm import LLMProvider
from synapse.protocols.planner import Planner
from synapse.protocols.tool import ToolRegistry
from synapse.protocols.memory import MemoryStore
from synapse.protocols.retriever import ContextRetriever
from synapse.protocols.sandbox import Sandbox


@pytest.mark.asyncio
async def test_agent_run_delegates_to_planner():
    container = Container()

    mock_llm = AsyncMock(spec=LLMProvider)
    mock_planner = AsyncMock(spec=Planner)
    mock_tools = AsyncMock(spec=ToolRegistry)
    mock_memory = AsyncMock(spec=MemoryStore)
    mock_retriever = AsyncMock(spec=ContextRetriever)
    mock_sandbox = AsyncMock(spec=Sandbox)
    event_bus = EventBus()

    from synapse.protocols.planner import AgentResult, ResultStatus
    mock_planner.execute.return_value = AgentResult(
        status=ResultStatus.SUCCESS,
        output="Task completed",
    )

    container.register(LLMProvider, mock_llm)
    container.register(Planner, mock_planner)
    container.register(ToolRegistry, mock_tools)
    container.register(MemoryStore, mock_memory)
    container.register(ContextRetriever, mock_retriever)
    container.register(Sandbox, mock_sandbox)
    container.register(EventBus, event_bus)

    agent = Agent(container)
    session = Session()
    result = await agent.run("test task", session)

    assert result.status == ResultStatus.SUCCESS
    assert result.output == "Task completed"
    mock_planner.execute.assert_called_once()


@pytest.mark.asyncio
async def test_agent_persists_memory_after_run():
    container = Container()

    mock_llm = AsyncMock(spec=LLMProvider)
    mock_planner = AsyncMock(spec=Planner)
    mock_tools = AsyncMock(spec=ToolRegistry)
    mock_memory = AsyncMock(spec=MemoryStore)
    mock_retriever = AsyncMock(spec=ContextRetriever)
    mock_sandbox = AsyncMock(spec=Sandbox)
    event_bus = EventBus()

    from synapse.protocols.planner import AgentResult, ResultStatus
    mock_planner.execute.return_value = AgentResult(
        status=ResultStatus.SUCCESS,
        output="Done",
    )

    container.register(LLMProvider, mock_llm)
    container.register(Planner, mock_planner)
    container.register(ToolRegistry, mock_tools)
    container.register(MemoryStore, mock_memory)
    container.register(ContextRetriever, mock_retriever)
    container.register(Sandbox, mock_sandbox)
    container.register(EventBus, event_bus)

    agent = Agent(container)
    result = await agent.run("test", Session())

    assert mock_memory.store.called


@pytest.mark.asyncio
async def test_fallback_context_event_returns_event_not_coroutine():
    """Regression: _fallback_context_event was ``async def`` but contained no
    await, so calling it returned a coroutine. event_bus.emit then crashed with
    ``AttributeError: 'coroutine' object has no attribute 'event_type'`` and
    the coroutine was orphaned (RuntimeWarning). It must return a real event."""
    import asyncio
    container = Container()

    mock_llm = AsyncMock(spec=LLMProvider)
    mock_planner = AsyncMock(spec=Planner)
    mock_tools = AsyncMock(spec=ToolRegistry)
    mock_memory = AsyncMock(spec=MemoryStore)
    mock_retriever = AsyncMock(spec=ContextRetriever)
    mock_sandbox = AsyncMock(spec=Sandbox)
    event_bus = EventBus()

    container.register(LLMProvider, mock_llm)
    container.register(Planner, mock_planner)
    container.register(ToolRegistry, mock_tools)
    container.register(MemoryStore, mock_memory)
    container.register(ContextRetriever, mock_retriever)
    container.register(Sandbox, mock_sandbox)
    container.register(EventBus, event_bus)

    agent = Agent(container)
    evt = agent._fallback_context_event(Session(), "test task")
    # Must NOT be a coroutine (the bug returned a coroutine).
    assert not asyncio.iscoroutine(evt)
    # emit reads .event_type; a coroutine had no such attr -> AttributeError.
    assert hasattr(evt, "event_type")
    # Emitting must not raise.
    await event_bus.emit(evt)


@pytest.mark.asyncio
async def test_context_retrieval_uses_configured_workspace_root(tmp_path):
    from synapse.modules.security.auth import ActionAuthorizer
    from synapse.protocols.planner import AgentResult, ResultStatus
    from synapse.protocols.retriever import Context

    container = Container()
    mock_planner = AsyncMock(spec=Planner)
    mock_planner.execute.return_value = AgentResult(ResultStatus.SUCCESS, "done")
    mock_retriever = AsyncMock(spec=ContextRetriever)
    mock_retriever.retrieve.return_value = Context()

    container.register(LLMProvider, AsyncMock(spec=LLMProvider))
    container.register(Planner, mock_planner)
    container.register(ToolRegistry, AsyncMock(spec=ToolRegistry))
    container.register(MemoryStore, AsyncMock(spec=MemoryStore))
    container.register(ContextRetriever, mock_retriever)
    container.register(Sandbox, AsyncMock(spec=Sandbox))
    container.register(EventBus, EventBus())
    container.register(ActionAuthorizer, type("Authorizer", (), {"workspace_root": str(tmp_path)})())

    await Agent(container).run("inspect the fixture", Session())
    assert mock_retriever.retrieve.await_args.kwargs["project_root"] == tmp_path.resolve()
