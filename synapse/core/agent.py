"""Core Agent -- assembles dependencies and delegates to Planner."""

import inspect

from synapse.core.container import Container
from synapse.core.session import Session
from synapse.protocols.llm import LLMProvider
from synapse.protocols.planner import Planner, AgentResult, ResultStatus
from synapse.protocols.tool import ToolRegistry
from synapse.protocols.memory import MemoryStore, MemoryLevel, MemoryEntry, MemoryMetadata
from synapse.protocols.retriever import ContextRetriever, ContextSource, Context, ContextBudget
from synapse.protocols.sandbox import Sandbox
from synapse.core.events import EventBus
from synapse.core.tokenizer import count_tokens

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

        # Optional context budget management
        self._partitioner: object | None = None
        self._compactor: object | None = None
        try:
            from synapse.modules.context.partitioner import ContextPartitioner
            self._partitioner = container.resolve(ContextPartitioner)
        except (KeyError, ImportError):
            pass
        try:
            from synapse.modules.context.compactor import ContextCompactor
            self._compactor = container.resolve(ContextCompactor)
        except (KeyError, ImportError):
            pass

        # Phase E — context config (optional, falls back to defaults)
        self._context_cfg = None
        try:
            from synapse.config.schema import SynapseConfig
            self._context_cfg = container.resolve(SynapseConfig)
        except (KeyError, ImportError):
            pass

        # Phase 3 — dynamic budget allocation (classifier + history)
        from synapse.modules.context.budget import BudgetHistory
        self._budget_history = BudgetHistory(project_memory=self.memory)
        self._last_task_type = None

        # Phase 4 — citation tracking state, populated during run().
        self._citation_tracker = None
        self._last_context = None

        # process-quality verification closed loop（可选服务）。
        self._quality_verifier = None
        try:
            from synapse.modules.process_quality import ProcessQualityVerifier
            self._quality_verifier = container.resolve(ProcessQualityVerifier)
        except (KeyError, ImportError):
            pass

    async def run(self, task: str, session: Session) -> AgentResult:
        run_id = self.event_bus.configure_run(trace_id=session.id)
        session.metadata["last_run_id"] = run_id
        # 1. Build context (Phase 3: task type drives budget selection)
        context = await self._build_context(task, session)
        self._last_context = context  # Phase 4 — retain for /context-report

        # 2. Annotate context with trust levels (Phase 3)
        if self._injection_guard is not None:
            context = self._injection_guard.annotate(context)  # type: ignore[union-attr]

        # 3. Delegate to planner (which will populate _citation_tracker)
        result = await self._planner.execute(
            task=task,
            context=context,
            tools=self.tools,
            llm=self.llm,
            sandbox=self.sandbox,
            session=session,
            event_bus=self.event_bus,
        )

        # Pick up the citation tracker the planner created, if any.
        self._citation_tracker = getattr(self._planner, "_last_citation_tracker", None)

        # Phase 3 — record citation history for adaptive budget tuning.
        if self._citation_tracker is not None and self._last_task_type is not None:
            try:
                report = self._citation_tracker.report(context)
                await self._budget_history.record(self._last_task_type, report)
            except Exception:
                pass

        # 4. Persist session memory
        await self._persist_memory(session, task, result)

        # TODO B — verify process quality; the hint is stored to PROJECT memory
        # and re-injected into the next task's prompt by the retriever.
        if self._quality_verifier is not None:
            try:
                await self._quality_verifier.after_task(
                    task, result.status == ResultStatus.SUCCESS,
                    session_id=session.id if session else "",
                )
            except Exception:
                pass

        return result

    async def _build_budget(self, task: str = "") -> "ContextBudget":
        """Construct a ContextBudget from config + task classification.

        Phase 3: classifies the task, picks the static profile, then
        applies historical adjustments if enough samples exist.
        """
        from synapse.protocols.retriever import ContextBudget
        from synapse.modules.context.classifier import classify_task, TaskType
        from synapse.modules.context.budget import select_budget

        if self._context_cfg is None:
            # No config — use the classifier against default total.
            task_type = classify_task(task) if task else TaskType.UNKNOWN
            self._last_task_type = task_type
            return select_budget(task_type, total_tokens=100_000)

        cfg = self._context_cfg.context
        total = cfg.total_tokens
        if total <= 0:
            total = self._context_cfg.planning.max_tokens_per_task

        task_type = classify_task(task) if task else TaskType.UNKNOWN
        self._last_task_type = task_type
        base = select_budget(task_type, total)

        # Apply historical adjustments (no-op until enough samples).
        return await self._budget_history.suggest_adjustment(task_type, base)

    def _resolve_compactor(self, overflow_chars: int):
        """Pick the compactor based on config strategy and overflow size."""
        from synapse.modules.context.compactor import ContextCompactor as _Trunc
        if self._context_cfg is None:
            return self._compactor if self._compactor is not None else _Trunc()
        strategy = self._context_cfg.context.compaction_strategy
        if strategy == "off":
            return None
        if strategy == "llm" and overflow_chars > self._context_cfg.context.llm_compact_threshold_chars:
            try:
                from synapse.modules.context.llm_compactor import LLMCompactor
                return LLMCompactor(llm=self.llm, fallback=_Trunc())
            except ImportError:
                return _Trunc()
        # truncation (default)
        return _Trunc()

    async def _build_context(self, task: str, session: Session = None):
        """Assemble context: retrieve → compact(overflow) → partition."""
        import asyncio
        from pathlib import Path
        from synapse.protocols.retriever import Context, ContextBlock, ContextSource

        budget = await self._build_budget(task)

        try:
            context = await asyncio.wait_for(
                self.retriever.retrieve(
                    task=task,
                    project_root=Path.cwd(),
                    tools=self.tools,
                    memory=self.memory,
                    budget=budget,
                ),
                timeout=10,
            )
        except asyncio.TimeoutError:
            # Fallback: minimal SYSTEM-only context (read README/AGENTS.md).
            await self.event_bus.emit(self._fallback_context_event(session, task))
            context = self._fallback_context()

        # Compact overflow before partitioning.
        overflow_chars = sum(len(b.content) for b in context.overflow)
        compactor = self._resolve_compactor(overflow_chars)
        if compactor is not None and context.overflow:
            # ponytail: LLMCompactor.compact is a coroutine — awaiting a
            # coroutine result is mandatory, otherwise context becomes a
            # coroutine (no .reference) and _build_context throws AttributeError.
            compacted = compactor.compact(context, budget)
            if inspect.iscoroutine(compacted):
                compacted = await compacted
            context = compacted
            # Fold the compacted overflow summaries back into `reference` so the
            # LLM actually consumes them — react.py does not inject the overflow
            # zone directly (ponytail: overflow is non-injected by design).
            context.reference = context.reference + context.overflow
            context.overflow = []
            # The folded summaries carry `derived_from`, so ContextPartitioner
            # treats them as protected and keeps them through budget trimming.

        # Apply budget via partitioner.
        if self._partitioner is not None:
            context = self._partitioner.partition(context, budget)  # type: ignore[union-attr]

        return context

    def _fallback_context(self) -> "Context":
        """Minimal SYSTEM-only context when retrieval times out."""
        from synapse.protocols.retriever import Context, ContextBlock, ContextSource
        from pathlib import Path

        system: list[ContextBlock] = []
        cwd = Path.cwd()
        for name in ("AGENTS.md", "CLAUDE.md", "README.md"):
            p = cwd / name
            if p.exists():
                try:
                    content = p.read_text(encoding="utf-8", errors="ignore")[:4000]
                    system.append(ContextBlock(
                        content=content,
                        source=ContextSource.MEMORY,
                        priority=9,
                        token_count=count_tokens(content),
                    ))
                except Exception:
                    pass
        return Context(system=system)

    def _fallback_context_event(self, session, task: str):
        """Emit a warning event when retrieval times out."""
        from synapse.protocols.events import AgentProgress
        return AgentProgress(
            session_id=session.id if session else "",
            phase="context_timeout",
            message="Context retrieval timed out; using minimal system context.",
        )

    async def _persist_memory(self, session: Session, task: str, result: AgentResult) -> None:
        """Store task summary as session memory (and semantic memory when available)."""
        import uuid
        from datetime import datetime

        content = f"Task: {task}\nResult ({result.status.value}): {result.output[:500]}"
        meta = MemoryMetadata(
            timestamp=datetime.now(),
            tags=["task-result", result.status.value],
            source_task=task,
        )
        entry = MemoryEntry(
            id=str(uuid.uuid4()),
            content=content,
            level=MemoryLevel.SESSION,
            metadata=meta,
        )
        await self.memory.store(entry)

        # SEMANTIC layer is optional (chromadb/qdrant). Write the same summary
        # so later tasks can retrieve it by similarity; a missing/erroring
        # backend must never break task completion.
        try:
            semantic_entry = MemoryEntry(
                id=str(uuid.uuid4()),
                content=content,
                level=MemoryLevel.SEMANTIC,
                metadata=meta,
            )
            await self.memory.store(semantic_entry)
        except Exception:
            pass
