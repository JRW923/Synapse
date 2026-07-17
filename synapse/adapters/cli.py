"""CLI entry point for Synapse."""

import argparse
import asyncio
import sys
from pathlib import Path

from synapse.config import load_config
from synapse.core.container import Container
from synapse.core.agent import Agent
from synapse.core.session import Session
from synapse.core.events import EventBus

from synapse.protocols.llm import LLMProvider
from synapse.protocols.planner import Planner, PlanningMode
from synapse.protocols.tool import ToolRegistry
from synapse.protocols.memory import MemoryStore
from synapse.protocols.retriever import ContextRetriever
from synapse.protocols.sandbox import Sandbox

# ---- Tools ----------------------------------------------------------------
from synapse.modules.tools.registry import DefaultToolRegistry
from synapse.modules.tools.file_read import ReadTool
from synapse.modules.tools.file_write import WriteTool
from synapse.modules.tools.file_edit import EditTool
from synapse.modules.tools.file_glob import GlobTool
from synapse.modules.tools.search_grep import GrepTool
from synapse.modules.tools.shell import ShellTool
from synapse.modules.tools.git_ import GitTool

# ---- Memory (Phase 1 + 2) --------------------------------------------------
from synapse.modules.memory.session import SessionMemory
from synapse.modules.memory.project import ProjectMemory
from synapse.modules.memory.user import UserMemory

# ---- Context (Phase 1 + 2) -------------------------------------------------
from synapse.modules.context.retriever import BasicContextRetriever
from synapse.modules.context.partitioner import ContextPartitioner
from synapse.modules.context.compactor import ContextCompactor

# ---- Security --------------------------------------------------------------
from synapse.modules.security.sandbox import ProcessSandbox
from synapse.modules.security.auth import ActionAuthorizer
from synapse.modules.security.audit import AuditLogger

# ---- Planning (Phase 1 + 2) -------------------------------------------------
from synapse.modules.planning.react import ReActPlanner
from synapse.modules.planning.plan_execute import PlanExecutePlanner
from synapse.modules.planning.hierarchical import HierarchicalPlanner

# ---- LLM Providers (all 5) --------------------------------------------------
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


# ---- LayeredMemory (Phase 2) -----------------------------------------------


class LayeredMemory:
    """Routes memory operations to the correct store based on MemoryLevel."""

    def __init__(self, session_memory, project_memory, user_memory):
        self._session = session_memory
        self._project = project_memory
        self._user = user_memory

    async def store(self, entry):
        from synapse.protocols.memory import MemoryLevel
        if entry.level == MemoryLevel.SESSION:
            await self._session.store(entry)
        elif entry.level == MemoryLevel.PROJECT:
            await self._project.store(entry)
        elif entry.level == MemoryLevel.USER:
            await self._user.store(entry)

    async def retrieve(self, query, level, top_k=5):
        if level.value == "session":  # MemoryLevel.SESSION
            return await self._session.retrieve(query, level, top_k)
        if level.value == "project":
            return await self._project.retrieve(query, level, top_k)
        if level.value == "user":
            return await self._user.retrieve(query, level, top_k)
        return []

    async def forget(self, entry_id):
        await self._session.forget(entry_id)
        await self._project.forget(entry_id)
        await self._user.forget(entry_id)


# ---- Provider resolution ---------------------------------------------------


def _create_provider(config):
    """Instantiate the LLM provider based on config.provider.provider."""
    provider_name = config.provider.provider.lower()
    cfg = config.provider

    if provider_name == "anthropic":
        return AnthropicProvider(
            model=cfg.model, api_key=cfg.api_key, max_tokens=cfg.max_tokens,
        )
    elif provider_name == "openai":
        if OpenAIProvider is None:
            raise ImportError("OpenAI SDK not installed. Run: pip install openai")
        return OpenAIProvider(
            model=cfg.model, api_key=cfg.api_key, max_tokens=cfg.max_tokens,
        )
    elif provider_name == "deepseek":
        if DeepSeekProvider is None:
            raise ImportError("DeepSeek provider requires the openai SDK. Run: pip install openai")
        return DeepSeekProvider(
            model=cfg.model, api_key=cfg.api_key, max_tokens=cfg.max_tokens,
        )
    elif provider_name == "google":
        if GoogleProvider is None:
            raise ImportError("Google AI SDK not installed. Run: pip install google-genai")
        return GoogleProvider(
            model=cfg.model, api_key=cfg.api_key, max_tokens=cfg.max_tokens,
        )
    elif provider_name == "ollama":
        if OllamaProvider is None:
            raise ImportError("Ollama provider requires the openai SDK. Run: pip install openai")
        return OllamaProvider(
            model=cfg.model, max_tokens=cfg.max_tokens,
        )
    else:
        raise ValueError(
            f"Unknown provider '{provider_name}'. "
            "Available: anthropic, openai, deepseek, google, ollama"
        )


# ---- Planner resolution ----------------------------------------------------


def _create_planner(config, auth):
    """Instantiate the planner based on config.planning.mode."""
    mode = config.planning.mode.lower()
    cfg = config.planning

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
        "Available: react, plan_execute, hierarchical"
    )


# ---- Container wiring ------------------------------------------------------


def build_container(config) -> Container:
    """Wire all Phase 1 + Phase 2 dependencies into the IoC container."""
    c = Container()

    # Core infrastructure
    event_bus = EventBus()
    c.register(EventBus, event_bus)

    # Audit log — tamper-evident event logging (Phase 2)
    audit_logger = AuditLogger(bus=event_bus)
    c.register(AuditLogger, audit_logger)

    # LLM Provider (selectable: anthropic, openai, deepseek, google, ollama)
    provider = _create_provider(config)
    c.register(LLMProvider, provider)

    # Tools
    registry = DefaultToolRegistry()
    registry.register(ReadTool())
    registry.register(WriteTool())
    registry.register(EditTool())
    registry.register(GlobTool())
    registry.register(GrepTool())
    registry.register(ShellTool())
    registry.register(GitTool())
    c.register(ToolRegistry, registry)

    # Memory — layered (Phase 2): session + project + user
    session_memory = SessionMemory()
    project_memory = ProjectMemory()
    user_memory = UserMemory()
    layered = LayeredMemory(session_memory, project_memory, user_memory)
    c.register(MemoryStore, layered)

    # Context retrieval (Phase 1)
    retriever = BasicContextRetriever()
    c.register(ContextRetriever, retriever)

    # Context budget management (Phase 2)
    partitioner = ContextPartitioner()
    compactor = ContextCompactor()
    # Partitioner and compactor are currently standalone — they will be
    # integrated into the Agent context pipeline in a future task.

    # Security
    sandbox = ProcessSandbox()
    c.register(Sandbox, sandbox)

    auth = ActionAuthorizer(
        workspace_root=config.tools.workspace_root,
        confirmation_enabled=config.security.auth_confirmation,
    )
    c.register(ActionAuthorizer, auth)

    # Planner (selectable: react, plan_execute, hierarchical)
    planner = _create_planner(config, auth)
    c.register(Planner, planner)

    return c


# ---- CLI entry point -------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        prog="synapse",
        description="Synapse — Connecting ideas into code",
    )
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Execute a task")
    run_parser.add_argument("task", nargs="+", help="Task description")
    run_parser.add_argument(
        "--provider", "-p",
        default=None,
        choices=["anthropic", "openai", "deepseek", "google", "ollama"],
        help="LLM provider (overrides config)",
    )
    run_parser.add_argument(
        "--model", "-m",
        default=None,
        help="Model name (overrides config)",
    )
    run_parser.add_argument(
        "--mode",
        default=None,
        choices=["react", "plan_execute", "hierarchical"],
        help="Planning mode (overrides config)",
    )

    sub.add_parser("version", help="Show version")

    args = parser.parse_args()

    if args.command == "version":
        from synapse import __version__
        print(f"Synapse v{__version__}")
        return

    if args.command == "run":
        task = " ".join(args.task)
        config = load_config()

        # CLI flags override config
        if args.provider:
            config.provider.provider = args.provider
        if args.model:
            config.provider.model = args.model
        if args.mode:
            config.planning.mode = args.mode

        container = build_container(config)
        agent = Agent(container)
        session = Session()

        async def execute():
            result = await agent.run(task, session)
            return result

        result = asyncio.run(execute())
        print(f"\n[Status: {result.status.value}]")
        print(result.output)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
