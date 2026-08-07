"""Synapse facade — clean Python API for agent creation and execution.

Usage:
    from synapse import Synapse

    agent = Synapse(provider="anthropic", model="claude-sonnet-4-6")
    result = await agent.run("Fix the bug in auth.py")
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from synapse.config import load_config
from synapse.core.container import Container
from synapse.core.agent import Agent
from synapse.core.session import Session
from synapse.core.events import EventBus
from synapse.protocols.events import BaseEvent

from synapse.protocols.llm import LLMProvider
from synapse.protocols.planner import Planner, AgentResult, PlanningMode
from synapse.protocols.tool import ToolRegistry
from synapse.protocols.memory import MemoryStore, MemoryEntry, MemoryLevel, MemoryMetadata
from synapse.eval.metrics import RunScore
from synapse.protocols.retriever import ContextRetriever
from synapse.protocols.sandbox import Sandbox

from synapse.modules.tools.registry import DefaultToolRegistry
from synapse.modules.tools.file_read import ReadTool
from synapse.modules.tools.file_write import WriteTool
from synapse.modules.tools.file_edit import EditTool
from synapse.modules.tools.file_glob import GlobTool
from synapse.modules.tools.search_grep import GrepTool
from synapse.modules.tools.shell import ShellTool
from synapse.modules.tools.git_ import GitTool

# External tools — optional dependencies (httpx, playwright, sqlite3)
try:
    from synapse.modules.tools.web import HTTPTool, WebFetchTool
except ImportError:  # pragma: no cover
    HTTPTool = None  # type: ignore[assignment]
    WebFetchTool = None  # type: ignore[assignment]

try:
    from synapse.modules.tools.web_search import WebSearchTool
except ImportError:  # pragma: no cover
    WebSearchTool = None  # type: ignore[assignment]

try:
    from synapse.modules.tools.db import DBTool
except ImportError:  # pragma: no cover
    DBTool = None  # type: ignore[assignment]

try:
    from synapse.modules.tools.browser import BrowserTool
except ImportError:  # pragma: no cover
    BrowserTool = None  # type: ignore[assignment]

from synapse.modules.memory.session import SessionMemory
from synapse.modules.memory.project import ProjectMemory
from synapse.modules.memory.user import UserMemory

try:
    from synapse.modules.memory.semantic import SemanticMemory
except ImportError:  # pragma: no cover
    SemanticMemory = None  # type: ignore[assignment]

try:
    from synapse.modules.memory.qdrant_backend import QdrantMemory
except ImportError:  # pragma: no cover
    QdrantMemory = None  # type: ignore[assignment]

from synapse.modules.context.retriever import BasicContextRetriever
from synapse.modules.context.partitioner import ContextPartitioner
from synapse.modules.context.compactor import ContextCompactor

from synapse.modules.security.sandbox import ProcessSandbox
from synapse.modules.security.auth import ActionAuthorizer
from synapse.modules.security.audit import AuditLogger
from synapse.modules.security.injection import InjectionGuard

# Planners — lightweight (protocols only, no SDKs).
from synapse.modules.planning.react import ReActPlanner
from synapse.modules.planning.plan_execute import PlanExecutePlanner
from synapse.modules.planning.hierarchical import HierarchicalPlanner
from synapse.modules.planning.swarm import SwarmPlanner
from synapse.modules.tools.background import get_default_manager
from synapse.modules.tools.skill_tool import SkillTool
from synapse.modules.skill import get_default_skill_loader
from synapse.modules.tools.todo_tool import TodoWriteTool, TodoReadTool
from synapse.modules.todo import get_default_todo_store

# MCP config type (lightweight dataclass).
from synapse.protocols.mcp import McpServerConfig


# ---- LayeredMemory ---------------------------------------------------------


class LayeredMemory:
    """Routes memory operations to the correct store based on MemoryLevel.

    Composes SessionMemory, ProjectMemory, UserMemory, and SemanticMemory
    into a single MemoryStore-compatible interface.  Each layer only handles
    its own level; queries for unhandled levels return an empty list.
    """

    def __init__(
        self,
        session_memory: SessionMemory,
        project_memory: ProjectMemory,
        user_memory: UserMemory,
        semantic_memory: SemanticMemory | None = None,
    ) -> None:
        self._session = session_memory
        self._project = project_memory
        self._user = user_memory
        self._semantic = semantic_memory

    async def store(self, entry: MemoryEntry) -> None:
        if entry.level == MemoryLevel.SESSION:
            await self._session.store(entry)
        elif entry.level == MemoryLevel.PROJECT:
            await self._project.store(entry)
        elif entry.level == MemoryLevel.USER:
            await self._user.store(entry)
        elif entry.level == MemoryLevel.SEMANTIC and self._semantic is not None:
            await self._semantic.store(entry)

    async def retrieve(
        self, query: str, level: MemoryLevel, top_k: int = 5
    ) -> list[MemoryEntry]:
        if level == MemoryLevel.SESSION:
            return await self._session.retrieve(query, level, top_k)
        if level == MemoryLevel.PROJECT:
            return await self._project.retrieve(query, level, top_k)
        if level == MemoryLevel.USER:
            return await self._user.retrieve(query, level, top_k)
        if level == MemoryLevel.SEMANTIC and self._semantic is not None:
            return await self._semantic.retrieve(query, level, top_k)
        return []

    async def forget(self, entry_id: str) -> None:
        await self._session.forget(entry_id)
        await self._project.forget(entry_id)
        await self._user.forget(entry_id)
        if self._semantic is not None:
            await self._semantic.forget(entry_id)


# ---- Provider registry -----------------------------------------------------
# Resolved lazily so that tests can patch module-level names after import.


def _resolve_provider(name: str, custom_providers: list | None = None, model: str = ""):
    """Return *(provider_class, base_url_override)* for *name*.

    Handles model-aware routing: ``deepseek-v4-*`` models use the Anthropic
    protocol against ``api.deepseek.com/anthropic``, while ``deepseek-chat``
    keeps the OpenAI-compatible ``api.deepseek.com/v1``.
    """
    # DeepSeek v4 models → Anthropic protocol on the Anthropic-compatible endpoint
    if name == "deepseek" and model.startswith("deepseek-v4"):
        import importlib
        mod = importlib.import_module("synapse.modules.providers.anthropic")
        return getattr(mod, "AnthropicProvider"), "https://api.deepseek.com/anthropic"

    _provider_modules: dict[str, str] = {
        "anthropic": "synapse.modules.providers.anthropic",
        "openai": "synapse.modules.providers.openai",
        "deepseek": "synapse.modules.providers.deepseek",
        "google": "synapse.modules.providers.google",
        "ollama": "synapse.modules.providers.ollama",
    }
    _provider_classes: dict[str, str] = {
        "anthropic": "AnthropicProvider",
        "openai": "OpenAIProvider",
        "deepseek": "DeepSeekProvider",
        "google": "GoogleProvider",
        "ollama": "OllamaProvider",
    }

    # Check custom providers first
    base_url = None
    if custom_providers:
        for cp in custom_providers:
            if cp.name == name:
                protocol = getattr(cp, "protocol", "openai") or "openai"
                base_url = cp.base_url
                if protocol == "anthropic":
                    name = "anthropic"
                else:
                    name = "openai"
                break

    mod_name = _provider_modules.get(name)
    cls_name = _provider_classes.get(name)
    if mod_name is None or cls_name is None:
        available = ", ".join(sorted(_provider_classes))
        raise ValueError(f"Unknown provider '{name}'.  Available: {available}")

    import importlib

    try:
        mod = importlib.import_module(mod_name)
    except ImportError as exc:
        raise ImportError(
            f"Provider '{name}' is not available — the required SDK "
            f"is not installed.  ({exc})"
        ) from exc
    return getattr(mod, cls_name), base_url


# ---- Synapse facade --------------------------------------------------------


class Synapse:
    """High-level facade for the Synapse agent framework.

    Wraps Container assembly and Agent creation so callers only need a
    single import and two method calls::

        from synapse import Synapse

        agent = Synapse(provider="anthropic", model="claude-sonnet-4-6")
        result = await agent.run("Fix the bug in auth.py")
        # -- or synchronously --
        result = agent.run_sync("Fix the bug in auth.py")

    Parameters
    ----------
    provider:
        LLM provider name.  One of ``anthropic`` (default), ``openai``,
        ``deepseek``, ``google``, ``ollama``.
    model:
        Model identifier string.  If *None*, the provider's default is used.
    config_path:
        Path to a YAML configuration file.  If *None*, the default lookup
        order is used (``synapse.yaml``, then ``~/.synapse/config.yaml``).
    enable_eval:
        If ``True``, wire eval metrics collectors to the event bus (Phase 3).
        Defaults to ``False``.
    memory_backend:
        Semantic memory backend.  One of ``"chromadb"`` (default) or
        ``"qdrant"``.
    enable_external_tools:
        If ``True``, register HTTPTool, DBTool, and BrowserTool in the tool
        registry.  These tools have ``RiskLevel.EXTERNAL`` and are disabled
        by default for safety.  Requires corresponding optional dependencies
        (``httpx``, ``playwright``).
    mcp_servers:
        Optional list of :class:`~synapse.protocols.mcp.McpServerConfig`
        objects describing MCP servers to connect.  Each server's tools are
        registered in the tool registry under ``mcp.<server_name>.<tool>``
        names.  Servers are connected during container assembly (at
        construction time).
    **overrides:
        Additional keyword arguments are applied as overrides on top of the
        loaded configuration.  Supported keys include any field on
        ``ProviderConfig``, ``PlanningConfig``, ``ToolsConfig``, or
        ``SecurityConfig`` (e.g. ``max_tokens``, ``max_iterations``,
        ``workspace_root``).
    """

    def __init__(
        self,
        provider: str = "anthropic",
        model: str | None = None,
        config_path: str | None = None,
        enable_eval: bool = False,
        memory_backend: str = "chromadb",
        enable_external_tools: bool = False,
        mcp_servers: list[McpServerConfig] | None = None,
        confirm_callback=None,  # async (AuthRequest) -> bool
        **overrides: Any,
    ) -> None:
        self._provider_name = provider
        self._enable_eval = enable_eval
        self._memory_backend = memory_backend
        self._enable_external_tools = enable_external_tools
        self._mcp_servers = mcp_servers
        self._mcp_manager = None  # populated in _build_container; connected lazily in run()
        self._confirm_callback = confirm_callback
        self._config = self._load_config(config_path, provider, model, overrides)
        self._container = self._build_container()
        self._last_run_score = None  # populated by run()
        self._last_process_hint = None  # L.4 — last process-quality hint

    # -- Public API ----------------------------------------------------------

    async def run(
        self,
        task: str,
        session: Session | None = None,
        confirm_callback=None,
    ) -> AgentResult:
        """Execute *task* asynchronously and return the result.

        Creates a fresh ``Session`` and ``Agent`` for each invocation,
        so multiple calls are independent.

        If *session* is provided it is used directly; otherwise a new
        ``Session`` is created.  This allows the HTTP server to retain a
        reference to the session for later inspection.

        *confirm_callback* (optional) overrides the instance-level callback
        for this single run — used by headless opt-ins (``run --yes``, server
        ``auto_approve``) so confirmation-required calls are approved without a
        human.  The planner is rebuilt with it and restored afterwards.
        """
        if session is None:
            session = Session()

        # L.4: the process-quality hint is per-run; clear it before this run.
        self._last_process_hint = None

        # L.3: rebuild the planner with a per-run confirm callback when given.
        override = confirm_callback is not None
        prev_planner = None
        if override:
            self._confirm_callback = confirm_callback
            auth = self._container.resolve(ActionAuthorizer)
            planner = self._create_planner(auth)
            prev_planner = self._container.resolve(Planner)
            self._container.register(type(planner), planner)

        # MCP: connect external servers on THIS event loop so their tools are
        # actually callable. The old container-build path connected on a
        # throwaway thread loop, so the receiver tasks died before any call.
        mcp_connected = False
        if self._mcp_manager is not None:
            await self._mcp_manager.connect_all(self._mcp_servers)
            mcp_connected = True

        try:
            agent = Agent(self._container)
            self._last_agent = agent   # Phase 4 — retained for /context-report

            # Runtime scoring: score each task independently, so reset the
            # per-run collectors before executing and snapshot them afterwards.
            for _m in self._run_metrics:
                _m.reset()

            result = await agent.run(task, session)

            status = result.status.value if hasattr(result.status, "value") else str(result.status)
            self._last_run_score = RunScore(
                task=task,
                status=status,
                safety=self._run_metrics[0].snapshot(),
                process=self._run_metrics[1].snapshot(),
                quality=self._run_metrics[2].snapshot(),
                efficiency=self._run_metrics[3].snapshot(),
            )
            await self._persist_run_score(self._last_run_score)
            return result
        finally:
            # ponytail: the planner swap is instance-wide and not safe under
            # concurrent requests (a parallel request could read the overridden
            # planner during this run).  A per-request Synapse would remove the
            # ceiling; for an explicit opt-in flag it is an acceptable trade-off.
            if override and prev_planner is not None:
                self._container.register(type(prev_planner), prev_planner)
            if mcp_connected and self._mcp_manager is not None:
                await self._mcp_manager.shutdown()

    def get_citation_report(self) -> dict | None:
        """Phase 4 — return the citation/usage report for the last run, or None."""
        agent = getattr(self, "_last_agent", None)
        if agent is None:
            return None
        tracker = getattr(agent, "_citation_tracker", None)
        context = getattr(agent, "_last_context", None)
        if tracker is None or context is None:
            return None
        return tracker.report(context)

    def get_run_score(self) -> dict | None:
        """Return the runtime score for the last run (or a live snapshot).

        The result is a serializable dict combining the four metric collectors
        (safety / process / quality / efficiency) plus the L.4 process-quality
        ``hint``.  Returns ``None`` only if the container was built without the
        runtime collectors.
        """
        if self._last_run_score is None and not getattr(self, "_run_metrics", None):
            return None
        if self._last_run_score is not None:
            score = self._last_run_score.to_dict()
        else:
            score = RunScore(
                safety=self._run_metrics[0].snapshot(),
                process=self._run_metrics[1].snapshot(),
                quality=self._run_metrics[2].snapshot(),
                efficiency=self._run_metrics[3].snapshot(),
            ).to_dict()
        score["process_hint"] = self._last_process_hint
        return score

    async def _on_process_quality_scored(self, event: BaseEvent) -> None:
        """L.4 — remember the latest process-quality hint for the user."""
        self._last_process_hint = getattr(event, "hint", None) or None

    async def _persist_run_score(self, score: RunScore) -> None:
        """Persist a run score to ProjectMemory as a rolling per-project log.

        Failures are swallowed so scoring never blocks a task.  All runs append
        to a single ``run-score-log`` entry, forming a queryable history.
        """
        try:
            pm = self._container.resolve(ProjectMemory)
        except Exception:
            return
        try:
            entry = MemoryEntry(
                id="run-score-log",
                content=json.dumps(score.to_dict(), ensure_ascii=False, indent=2),
                level=MemoryLevel.PROJECT,
                metadata=MemoryMetadata(
                    timestamp=datetime.now(), priority=3, tags=["run_score"],
                ),
            )
            await pm.store(entry)
        except Exception:
            return

    def run_sync(self, task: str) -> AgentResult:
        """Synchronous wrapper around :meth:`run`.

        Uses ``asyncio.run`` internally and is therefore not suitable for
        use inside an already-running event loop.
        """
        return asyncio.run(self.run(task))

    # -- Internal: config ----------------------------------------------------

    @staticmethod
    def _load_config(
        config_path: str | None,
        provider: str,
        model: str | None,
        overrides: dict[str, Any],
    ):
        config, _ = load_config(config_path)

        # Explicit positional arguments
        config.provider.provider = provider
        if model is not None:
            config.provider.model = model

        # **overrides applied to matching config sections
        for key, value in overrides.items():
            if hasattr(config.provider, key):
                setattr(config.provider, key, value)
            elif hasattr(config.planning, key):
                setattr(config.planning, key, value)
            elif hasattr(config.tools, key):
                setattr(config.tools, key, value)
            elif hasattr(config.security, key):
                setattr(config.security, key, value)

        return config

    # -- Internal: container assembly ----------------------------------------

    def _build_container(self) -> Container:
        """Wire every Phase-1 and Phase-2 module into the IoC container."""
        c = Container()

        # Core infrastructure
        event_bus = EventBus()
        c.register(EventBus, event_bus)

        # s04 — user hooks: run shell commands after the configured events fire.
        if self._config.hooks.hooks:
            from synapse.modules.hooks import HookRunner
            HookRunner(self._config.hooks.hooks).attach(event_bus)

        # Make config available to components that need it (e.g. Agent reads ContextConfig).
        from synapse.config.schema import SynapseConfig
        c.register(SynapseConfig, self._config)

        # Audit log — tamper-evident event logging (Phase 2)
        audit_logger = AuditLogger(bus=event_bus)
        c.register(AuditLogger, audit_logger)

        # LLM Provider (selected by name)
        provider = self._create_provider()
        c.register(LLMProvider, provider)

        # Tools
        registry = DefaultToolRegistry()
        for tool in self._create_all_tools():
            registry.register(tool)

        # MCP — create the manager but do NOT connect here. MCP clients must
        # live on the same event loop that later calls their tools, so the
        # connection is deferred to run() (see _mcp_manager usage there).
        mcp_manager = None
        if self._mcp_servers:
            from synapse.modules.mcp.manager import McpManager as _McpManager
            mcp_manager = _McpManager(tool_registry=registry, event_bus=event_bus)
        self._mcp_manager = mcp_manager

        c.register(ToolRegistry, registry)

        # Memory — layered: session + project + user + semantic
        session_memory = SessionMemory()
        project_memory = ProjectMemory()
        user_memory = UserMemory()
        semantic_memory = self._create_semantic_memory()
        layered = LayeredMemory(session_memory, project_memory, user_memory, semantic_memory)
        c.register(MemoryStore, layered)

        # Process-quality verification closed loop — live, not eval-gated.
        # Skip persisting feedback during eval benchmarks so the loop does not
        # leak into later benchmark tasks' prompts.
        from synapse.modules.process_quality import ProcessQualityVerifier
        c.register(
            ProcessQualityVerifier,
            ProcessQualityVerifier(
                event_bus=event_bus,
                memory=layered,
                persist_feedback=not self._enable_eval,
            ),
        )

        # L.4: capture the process-quality hint (emitted per task) so it can be
        # surfaced to the user via get_run_score() / CLI /score / server /run.
        event_bus.subscribe("process_quality_scored", self._on_process_quality_scored)

        # Context
        retriever = BasicContextRetriever()
        c.register(ContextRetriever, retriever)

        # Context budget management
        partitioner = ContextPartitioner()
        compactor = ContextCompactor()
        c.register(ContextPartitioner, partitioner)
        c.register(ContextCompactor, compactor)

        # Security
        sandbox = ProcessSandbox()
        c.register(Sandbox, sandbox)

        auth = ActionAuthorizer(
            workspace_root=self._config.tools.workspace_root,
            confirmation_enabled=self._config.security.auth_confirmation,
            # EXTERNAL tools (web/browser/db) run only when explicitly opted in
            # via config (security.allow_external) or the enable_external_tools
            # flag (which also registers them). web_search is READ_ONLY and is
            # not affected by this switch.
            allow_external=self._config.security.allow_external or self._enable_external_tools,
        )
        c.register(ActionAuthorizer, auth)

        # Injection guard — annotates context blocks with trust levels
        injection_guard = InjectionGuard()
        c.register(InjectionGuard, injection_guard)

        # Runtime scoring: all four EventBus-driven metric collectors
        # are ALWAYS wired so every task yields an observable process/quality/
        # efficiency/safety score.  They are cheap counters only — gating them
        # behind _enable_eval meant real runs produced no scores.  _enable_eval
        # still toggles the heavier experiment harness elsewhere.
        from synapse.eval.metrics.safety import SafetyMetrics as _SM
        from synapse.eval.metrics.process import ProcessMetrics as _PM
        from synapse.eval.metrics.quality import QualityMetrics as _QM
        from synapse.eval.metrics.efficiency import EfficiencyMetrics as _EM
        self._run_metrics = [
            _SM(bus=event_bus, workspace_root=self._config.tools.workspace_root),
            _PM(bus=event_bus),
            _QM(bus=event_bus),
            _EM(bus=event_bus),
        ]
        for _m in self._run_metrics:
            c.register(type(_m), _m)

        # Planner (selected by planning mode)
        planner = self._create_planner(auth)
        c.register(Planner, planner)

        return c

    # -- Internal: provider factory ------------------------------------------

    def _create_provider(self):
        """Instantiate the configured LLM provider."""
        provider_name = self._config.provider.provider.lower()
        cfg = self._config.provider

        provider_cls, base_url = _resolve_provider(provider_name, cfg.custom_providers, cfg.model)
        kwargs: dict = dict(
            model=cfg.model,
            api_key=cfg.api_key,
            max_tokens=cfg.max_tokens,
            timeout_seconds=cfg.timeout_seconds,
        )
        if base_url:
            kwargs["base_url"] = base_url
        return provider_cls(**kwargs)

    # -- Internal: semantic memory factory -----------------------------------

    def _create_semantic_memory(self):
        """Instantiate the configured semantic memory backend.

        Returns
        -------
        SemanticMemory | QdrantMemory | None
            The semantic memory store instance, or *None* if the backend
            is not available.
        """
        backend = self._memory_backend.lower()

        if backend == "chromadb":
            if SemanticMemory is None:
                raise ImportError(
                    "SemanticMemory (chromadb) is not available — "
                    "install chromadb to use it."
                )
            return SemanticMemory()

        if backend == "qdrant":
            if QdrantMemory is None:
                raise ImportError(
                    "QdrantMemory is not available — "
                    "install qdrant-client to use it."
                )
            return QdrantMemory()

        raise ValueError(
            f"Unknown memory_backend '{backend}'. "
            f"Available: chromadb, qdrant"
        )

    # -- Internal: planner factory -------------------------------------------

    def _create_planner(self, auth: ActionAuthorizer) -> Planner:
        """Instantiate the configured planning strategy."""
        return build_planner(self._config.planning, auth, self._confirm_callback)

    # -- Internal: tool factory ----------------------------------------------

    def _create_all_tools(self):
        """Return the full set of tools, including external ones when enabled."""
        # s13 — shared so ShellTool can start background tasks the planner tracks.
        bg_manager = get_default_manager()
        # s07 — shared so prompt injection and the load_skill tool agree.
        skill_loader = get_default_skill_loader()
        tools: list = [
            ReadTool(),
            WriteTool(workspace_root=self._config.tools.workspace_root),
            EditTool(),
            GlobTool(),
            GrepTool(),
            ShellTool(background_manager=bg_manager),
            GitTool(),
            SkillTool(skill_loader),
            TodoWriteTool(get_default_todo_store()),
            TodoReadTool(get_default_todo_store()),
        ]

        # Web search is always available (DuckDuckGo, no API key required).
        if WebSearchTool is not None:
            tools.append(WebSearchTool())

        # Read-only URL fetch is always available (GET-only, READ_ONLY). It lets
        # the LLM read a specific page without scripting python/curl. Heavier
        # external tools (web/browser/db) stay gated behind allow_external.
        if WebFetchTool is not None:
            tools.append(WebFetchTool())

        if self._enable_external_tools or self._config.security.allow_external:
            if HTTPTool is not None:
                tools.append(HTTPTool())
            if DBTool is not None:
                tools.append(DBTool(
                    workspace_root=self._config.tools.workspace_root,
                ))
            if BrowserTool is not None:
                tools.append(BrowserTool())

        return tools


def build_planner(planning_config, auth, confirm_callback=None) -> Planner:
    """Build the planner for a planning config — single source of truth.

    Shared by :meth:`Synapse._create_planner` and by CLI/test wiring so the
    mode->Planner mapping lives in exactly one place.
    """
    mode = planning_config.mode.lower()
    cfg = planning_config

    react = ReActPlanner(
        max_iterations=cfg.max_iterations,
        thrashing_threshold=cfg.thrashing_threshold,
        max_thrashing_events=cfg.max_thrashing_events,
        max_tokens_per_task=cfg.max_tokens_per_task,
        auth=auth,
        confirm_callback=confirm_callback,
        total_timeout_seconds=cfg.total_timeout_seconds,
        max_tool_result_chars=cfg.max_tool_result_chars,
        background_manager=get_default_manager(),
        skill_loader=get_default_skill_loader(),
    )

    if mode == PlanningMode.REACT:
        return react
    if mode == PlanningMode.PLAN_EXECUTE:
        return PlanExecutePlanner(react_planner=react)
    if mode == PlanningMode.HIERARCHICAL:
        complex_planner = PlanExecutePlanner(react_planner=react)
        return HierarchicalPlanner(react_planner=react, complex_planner=complex_planner)
    if mode == PlanningMode.SWARM:
        # s18 — give swarm workers filesystem isolation when we know the root.
        wt = None
        if isinstance(auth, ActionAuthorizer):
            from synapse.modules.planning.worktree import WorktreeManager
            wt = WorktreeManager(auth.workspace_root)
        return SwarmPlanner(react_planner=react, worktree_manager=wt)

    raise ValueError(
        f"Unknown planning mode '{mode}'. "
        f"Available: react, plan_execute, hierarchical, swarm"
    )
