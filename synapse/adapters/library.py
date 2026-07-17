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

from synapse.modules.memory.session import SessionMemory
from synapse.modules.memory.project import ProjectMemory
from synapse.modules.memory.user import UserMemory

from synapse.modules.context.retriever import BasicContextRetriever
from synapse.modules.context.partitioner import ContextPartitioner
from synapse.modules.context.compactor import ContextCompactor

from synapse.modules.security.sandbox import ProcessSandbox
from synapse.modules.security.auth import ActionAuthorizer
from synapse.modules.security.audit import AuditLogger

# LLM Providers — imported at module level so tests can patch them.
# Anthropic is the default and always required; others are optional.
from synapse.modules.providers.anthropic import AnthropicProvider

try:
    from synapse.modules.providers.openai import OpenAIProvider
except ImportError:  # pragma: no cover
    OpenAIProvider = None  # type: ignore[assignment]

try:
    from synapse.modules.providers.deepseek import DeepSeekProvider
except ImportError:  # pragma: no cover
    DeepSeekProvider = None  # type: ignore[assignment]

try:
    from synapse.modules.providers.google import GoogleProvider
except ImportError:  # pragma: no cover
    GoogleProvider = None  # type: ignore[assignment]

try:
    from synapse.modules.providers.ollama import OllamaProvider
except ImportError:  # pragma: no cover
    OllamaProvider = None  # type: ignore[assignment]

# Planners
from synapse.modules.planning.react import ReActPlanner
from synapse.modules.planning.plan_execute import PlanExecutePlanner
from synapse.modules.planning.hierarchical import HierarchicalPlanner


# ---- LayeredMemory ---------------------------------------------------------


class LayeredMemory:
    """Routes memory operations to the correct store based on MemoryLevel.

    Composes SessionMemory, ProjectMemory, and UserMemory into a single
    MemoryStore-compatible interface.  Each layer only handles its own level;
    queries for unhandled levels return an empty list.
    """

    def __init__(
        self,
        session_memory: SessionMemory,
        project_memory: ProjectMemory,
        user_memory: UserMemory,
    ) -> None:
        self._session = session_memory
        self._project = project_memory
        self._user = user_memory

    async def store(self, entry: MemoryEntry) -> None:
        if entry.level == MemoryLevel.SESSION:
            await self._session.store(entry)
        elif entry.level == MemoryLevel.PROJECT:
            await self._project.store(entry)
        elif entry.level == MemoryLevel.USER:
            await self._user.store(entry)

    async def retrieve(
        self, query: str, level: MemoryLevel, top_k: int = 5
    ) -> list[MemoryEntry]:
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


# ---- Provider registry -----------------------------------------------------
# Resolved lazily so that tests can patch module-level names after import.


def _resolve_provider(name: str):
    """Return the provider class for *name*, resolved at call time."""
    _providers: dict[str, str] = {
        "anthropic": "AnthropicProvider",
        "openai": "OpenAIProvider",
        "deepseek": "DeepSeekProvider",
        "google": "GoogleProvider",
        "ollama": "OllamaProvider",
    }
    attr = _providers.get(name)
    if attr is None:
        raise ValueError(
            f"Unknown provider '{name}'. "
            f"Available: {', '.join(sorted(_providers))}"
        )
    cls = globals().get(attr)
    if cls is None:
        raise ImportError(
            f"Provider '{name}' is not available — the required SDK is not installed."
        )
    return cls


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
        Model identifier string.  If *None*, the provider’s default is used.
    config_path:
        Path to a YAML configuration file.  If *None*, the default lookup
        order is used (``synapse.yaml``, then ``~/.synapse/config.yaml``).
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
        **overrides: Any,
    ) -> None:
        self._provider_name = provider
        self._config = self._load_config(config_path, provider, model, overrides)
        self._container = self._build_container()

    # -- Public API ----------------------------------------------------------

    async def run(self, task: str) -> AgentResult:
        """Execute *task* asynchronously and return the result.

        Creates a fresh ``Session`` and ``Agent`` for each invocation,
        so multiple calls are independent.
        """
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
        config = load_config(config_path)

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
        c.register(ToolRegistry, registry)

        # Memory — layered: session + project + user
        session_memory = SessionMemory()
        project_memory = ProjectMemory()
        user_memory = UserMemory()
        layered = LayeredMemory(session_memory, project_memory, user_memory)
        c.register(MemoryStore, layered)

        # Context
        retriever = BasicContextRetriever()
        c.register(ContextRetriever, retriever)

        # Context budget management (Phase 2)
        partitioner = ContextPartitioner()
        compactor = ContextCompactor()

        # Security
        sandbox = ProcessSandbox()
        c.register(Sandbox, sandbox)

        auth = ActionAuthorizer(
            workspace_root=self._config.tools.workspace_root,
            confirmation_enabled=self._config.security.auth_confirmation,
        )
        c.register(ActionAuthorizer, auth)

        # Planner (selected by planning mode)
        planner = self._create_planner(auth)
        c.register(Planner, planner)

        return c

    # -- Internal: provider factory ------------------------------------------

    def _create_provider(self):
        """Instantiate the configured LLM provider."""
        provider_name = self._config.provider.provider.lower()
        cfg = self._config.provider

        provider_cls = _resolve_provider(provider_name)
        return provider_cls(
            model=cfg.model,
            api_key=cfg.api_key,
            max_tokens=cfg.max_tokens,
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
            auth=auth,
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

    @staticmethod
    def _create_all_tools():
        """Return the full set of built-in tools."""
        return [
            ReadTool(),
            WriteTool(),
            EditTool(),
            GlobTool(),
            GrepTool(),
            ShellTool(),
            GitTool(),
        ]
