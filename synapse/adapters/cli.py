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
from synapse.modules.security.auth import ActionAuthorizer

from synapse.protocols.llm import LLMProvider
from synapse.protocols.planner import Planner
from synapse.protocols.tool import ToolRegistry
from synapse.protocols.memory import MemoryStore
from synapse.protocols.retriever import ContextRetriever
from synapse.protocols.sandbox import Sandbox


def build_container(config) -> Container:
    """Wire all dependencies into the IoC container."""
    c = Container()

    # Core infrastructure
    event_bus = EventBus()
    c.register(EventBus, event_bus)

    # LLM Provider
    provider = AnthropicProvider(
        model=config.provider.model,
        api_key=config.provider.api_key,
    )
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

    # Memory
    memory = SessionMemory()
    c.register(MemoryStore, memory)

    # Context
    retriever = BasicContextRetriever()
    c.register(ContextRetriever, retriever)

    # Security
    sandbox = ProcessSandbox()
    c.register(Sandbox, sandbox)

    # Planner
    planner = ReActPlanner(max_iterations=config.planning.max_iterations)
    c.register(Planner, planner)

    return c


def main():
    parser = argparse.ArgumentParser(
        prog="synapse",
        description="Synapse — Connecting ideas into code",
    )
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Execute a task")
    run_parser.add_argument("task", nargs="+", help="Task description")

    sub.add_parser("version", help="Show version")

    args = parser.parse_args()

    if args.command == "version":
        from synapse import __version__
        print(f"Synapse v{__version__}")
        return

    if args.command == "run":
        task = " ".join(args.task)
        config = load_config()
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
