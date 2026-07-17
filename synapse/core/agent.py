"""Core Agent -- assembles dependencies and delegates to Planner."""

from synapse.core.container import Container
from synapse.core.session import Session
from synapse.protocols.llm import LLMProvider
from synapse.protocols.planner import Planner, AgentResult
from synapse.protocols.tool import ToolRegistry
from synapse.protocols.memory import MemoryStore, MemoryLevel, MemoryEntry, MemoryMetadata
from synapse.protocols.retriever import ContextRetriever, ContextSource, Context
from synapse.protocols.sandbox import Sandbox
from synapse.core.events import EventBus

# Lazy import — InjectionGuard lives in modules/, not core/.
# It is resolved from the container at runtime when available.
try:
    from synapse.modules.security.injection import InjectionGuard as _InjectionGuard
except ImportError:  # pragma: no cover
    _InjectionGuard = None  # type: ignore[assignment]


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

        # Phase 3: optional InjectionGuard for context trust annotation
        self._injection_guard: object | None = None
        if _InjectionGuard is not None:
            try:
                self._injection_guard = container.resolve(_InjectionGuard)
            except KeyError:
                pass

    async def run(self, task: str, session: Session) -> AgentResult:
        # 1. Build context
        context = await self._build_context(task)

        # 2. Annotate context with trust levels (Phase 3)
        if self._injection_guard is not None:
            context = self._injection_guard.annotate(context)  # type: ignore[union-attr]

        # 3. Delegate to planner
        result = await self._planner.execute(
            task=task,
            context=context,
            tools=self.tools,
            llm=self.llm,
            sandbox=self.sandbox,
            session=session,
            event_bus=self.event_bus,
        )

        # 4. Persist session memory
        await self._persist_memory(session, task, result)

        return result

    async def _build_context(self, task: str):
        """Assemble context from retriever + memory."""
        import asyncio
        from pathlib import Path
        try:
            return await asyncio.wait_for(
                self.retriever.retrieve(
                    task=task,
                    project_root=Path.cwd(),
                    tools=self.tools,
                    memory=self.memory,
                ),
                timeout=5,  # context 构建不应超过 5 秒
            )
        except asyncio.TimeoutError:
            # 大型目录下 grep/glob 可能很慢，返回空上下文比卡死好
            from synapse.protocols.retriever import Context
            return Context()

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
