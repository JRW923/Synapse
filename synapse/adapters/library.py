"""Synapse facade — clean Python API for agent creation and execution.

Usage:
    from synapse import Synapse

    agent = Synapse(provider="anthropic", model="claude-sonnet-4-6")
    result = await agent.run("Fix the bug in auth.py")
"""

from __future__ import annotations

import asyncio
from typing import Any

from synapse.config import load_config
from synapse.core.container import Container
from synapse.core.agent import Agent
from synapse.core.session import Session
from synapse.core.events import EventBus

from synapse.protocols.llm import LLMProvider
from synapse.protocols.planner import Planner, AgentResult, PlanningMode
from synapse.protocols.tool import ToolRegistry
from synapse.protocols.memory import MemoryStore, MemoryEntry, MemoryLevel
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
    from synapse.modules.tools.web import HTTPTool
except ImportError:  # pragma: no cover
    HTTPTool = None  # type: ignore[assignment]

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
        self._confirm_callback = confirm_callback
        self._config = self._load_config(config_path, provider, model, overrides)
        self._container = self._build_container()

    # -- Public API ----------------------------------------------------------

    async def run(self, task: str, session: Session | None = None) -> AgentResult:
        """Execute *task* asynchronously and return the result.

        Creates a fresh ``Session`` and ``Agent`` for each invocation,
        so multiple calls are independent.

        If *session* is provided it is used directly; otherwise a new
        ``Session`` is created.  This allows the HTTP server to retain a
        reference to the session for later inspection.
        """
        if session is None:
            session = Session()
        agent = Agent(self._container)
        return await agent.run(task, session)

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

        # MCP — Connect external MCP servers and register their tools
        mcp_manager = None
        if self._mcp_servers:
            from synapse.modules.mcp.manager import McpManager as _McpManager
            mcp_manager = _McpManager(tool_registry=registry, event_bus=event_bus)
            self._connect_mcp_servers_sync(mcp_manager, self._mcp_servers)

        c.register(ToolRegistry, registry)

        # Memory — layered: session + project + user + semantic
        session_memory = SessionMemory()
        project_memory = ProjectMemory()
        user_memory = UserMemory()
        semantic_memory = self._create_semantic_memory()
        layered = LayeredMemory(session_memory, project_memory, user_memory, semantic_memory)
        c.register(MemoryStore, layered)

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
        )
        c.register(ActionAuthorizer, auth)

        # Injection guard — annotates context blocks with trust levels
        injection_guard = InjectionGuard()
        c.register(InjectionGuard, injection_guard)

        # Eval — optionally wire metrics collectors to EventBus (Phase 3)
        if self._enable_eval:
            from synapse.eval.metrics.process import ProcessMetrics as _PM
            from synapse.eval.metrics.quality import QualityMetrics as _QM
            from synapse.eval.metrics.efficiency import EfficiencyMetrics as _EM
            from synapse.eval.metrics.safety import SafetyMetrics as _SM
            c.register(_PM, _PM(bus=event_bus))
            c.register(_QM, _QM(bus=event_bus))
            c.register(_EM, _EM(bus=event_bus))
            c.register(_SM, _SM(
                bus=event_bus,
                workspace_root=self._config.tools.workspace_root,
            ))

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
        mode = self._config.planning.mode.lower()
        cfg = self._config.planning

        # Base ReAct planner — used by all three modes
        react = ReActPlanner(
            max_iterations=cfg.max_iterations,
            thrashing_threshold=cfg.thrashing_threshold,
            max_thrashing_events=cfg.max_thrashing_events,
            max_tokens_per_task=cfg.max_tokens_per_task,
            auth=auth,
            confirm_callback=self._confirm_callback,
            total_timeout_seconds=cfg.total_timeout_seconds,
        )

        if mode == PlanningMode.REACT:
            return react

        if mode == PlanningMode.PLAN_EXECUTE:
            return PlanExecutePlanner(react_planner=react)

        if mode == PlanningMode.HIERARCHICAL:
            complex_planner = PlanExecutePlanner(react_planner=react)
            return HierarchicalPlanner(
                react_planner=react,
                complex_planner=complex_planner,
            )

        raise ValueError(
            f"Unknown planning mode '{mode}'. "
            f"Available: react, plan_execute, hierarchical"
        )

    # -- Internal: tool factory ----------------------------------------------

    def _create_all_tools(self):
        """Return the full set of tools, including external ones when enabled."""
        tools: list = [
            ReadTool(),
            WriteTool(),
            EditTool(),
            GlobTool(),
            GrepTool(),
            ShellTool(),
            GitTool(),
        ]

        if self._enable_external_tools:
            if HTTPTool is not None:
                tools.append(HTTPTool())
            if DBTool is not None:
                tools.append(DBTool(
                    workspace_root=self._config.tools.workspace_root,
                ))
            if BrowserTool is not None:
                tools.append(BrowserTool())

        return tools

    # -- Internal: MCP server connection --------------------------------------

    @staticmethod
    def _connect_mcp_servers_sync(
        manager: "McpManager",
        servers: list[McpServerConfig],
    ) -> None:
        """Connect *manager* to every server in *servers* synchronously.

        Uses :func:`asyncio.run` in a thread-pool executor when called from
        inside a running event loop (e.g. during pytest-asyncio tests).
        When no event loop is running it calls :func:`asyncio.run` directly.
        """
        import asyncio
        import concurrent.futures

        async def _connect_all() -> None:
            for server_config in servers:
                await manager.add_server(server_config)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — safe to call asyncio.run directly
            asyncio.run(_connect_all())
            return

        # A loop is already running — run the coroutine in a dedicated thread
        # so that it can create its own fresh event loop.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(asyncio.run, _connect_all())
            future.result(timeout=60)
