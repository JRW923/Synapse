"""Core Agent -- assembles dependencies and delegates to Planner."""

from synapse.core.container import Container
from synapse.core.session import Session
from synapse.protocols.llm import LLMProvider
from synapse.protocols.planner import Planner, AgentResult
from synapse.protocols.tool import ToolRegistry
from synapse.protocols.memory import MemoryStore, MemoryLevel, MemoryEntry, MemoryMetadata
from synapse.protocols.retriever import ContextRetriever, ContextSource
from synapse.protocols.sandbox import Sandbox
from synapse.core.events import EventBus


class Agent:
    """Dependency injection assembler -- wires components and delegates to Planner.

    The Agent does NOT implement the execution loop. That belongs to the Planner.
    This separation allows swapping planning strategies (ReAct, Plan-Execute, etc.)
    without touching the Agent core.
    """

    def __init__(self, container: Container):
        self.llm: LLMProvider = container.resolve(LLMProvider)
        self.tools: ToolRegistry = container.resolve(ToolRegistry)
        self.memory: MemoryStore = container.resolve(MemoryStore)
        self.retriever: ContextRetriever = container.resolve(ContextRetriever)
        self.sandbox: Sandbox = container.resolve(Sandbox)
        self.event_bus: EventBus = container.resolve(EventBus)
        self._planner: Planner = container.resolve(Planner)

    async def run(self, task: str, session: Session) -> AgentResult:
        # 1. Build context
        context = await self._build_context(task)

        # 2. Delegate to planner
        result = await self._planner.execute(
            task=task,
            context=context,
            tools=self.tools,
            llm=self.llm,
            sandbox=self.sandbox,
            session=session,
            event_bus=self.event_bus,
        )

        # 3. Persist session memory
        await self._persist_memory(session, task, result)

        return result

    async def _build_context(self, task: str):
        """Assemble context from retriever + memory."""
        from pathlib import Path
        return await self.retriever.retrieve(
            task=task,
            project_root=Path.cwd(),
            tools=self.tools,
            memory=self.memory,
        )

    async def _persist_memory(self, session: Session, task: str, result: AgentResult) -> None:
        """Store task summary as session memory."""
        import uuid
        from datetime import datetime

        entry = MemoryEntry(
            id=str(uuid.uuid4()),
            content=f"Task: {task}\nResult ({result.status.value}): {result.output[:500]}",
            level=MemoryLevel.SESSION,
            metadata=MemoryMetadata(
                timestamp=datetime.now(),
                tags=["task-result", result.status.value],
                source_task=task,
            ),
        )
        await self.memory.store(entry)
