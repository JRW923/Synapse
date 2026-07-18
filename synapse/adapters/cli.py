"""CLI entry point for Synapse."""

import argparse
import asyncio
import sys
from pathlib import Path

from synapse.config import load_config
from synapse.protocols.mcp import McpServerConfig
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


def _parse_mcp_servers(raw_values: list[str] | None) -> list[McpServerConfig] | None:
    """Parse ``--mcp-server`` values into a list of :class:`McpServerConfig`.

    Format: ``name:command_or_url``

    - If the part after ``:`` starts with ``http://`` or ``https://``,
      the transport is ``streamable_http``.
    - Otherwise the transport is ``stdio``; the command and its arguments
      are split on whitespace.
    """
    if not raw_values:
        return None

    configs: list[McpServerConfig] = []
    for raw in raw_values:
        if ":" not in raw:
            raise ValueError(
                f"Invalid --mcp-server format: {raw!r}.  "
                f"Expected 'name:command' or 'name:http://...'."
            )

        name, rest = raw.split(":", 1)

        if rest.startswith("http://") or rest.startswith("https://"):
            configs.append(McpServerConfig(
                name=name,
                transport="streamable_http",
                url=rest,
            ))
        else:
            parts = rest.split()
            command = parts[0]
            args = parts[1:] if len(parts) > 1 else []
            configs.append(McpServerConfig(
                name=name,
                transport="stdio",
                command=command,
                args=args,
            ))

    return configs


def _check_api_key(config):
    """Warn and show setup guide if no API key is configured."""
    if config.provider.api_key:
        return

    provider = config.provider.provider
    env_vars = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "google": "GOOGLE_API_KEY",
        "ollama": None,  # Ollama runs locally, no key needed
    }
    env_var = env_vars.get(provider)

    if env_var:
        import os
        if os.environ.get(env_var):
            return  # found in env

        print(f"\n  No API key found for '{provider}'.\n")
        print(f"  Set it with one of:\n")
        print(f"    1. Environment:  set {env_var}=sk-your-key    (Windows CMD)")
        print(f"                     $env:{env_var} = \"sk-...\"    (PowerShell)")
        print(f"    2. Config file:  echo 'provider:'  > synapse.yaml")
        print(f"                     echo '  api_key: sk-...' >> synapse.yaml")
        print(f"    3. User config:  same format at ~/.synapse/config.yaml")
        print()


def _make_confirm_callback(pause_event=None, status_holder=None):
    """Return an async callback that prompts the user for tool-call approval."""
    import sys as _sys

    async def _confirm(request):
        # Pause Rich spinner if active
        st = None
        if status_holder is not None and len(status_holder) > 0:
            st = status_holder[0]
            if st is not None:
                st.stop()

        if pause_event is not None:
            pause_event.clear()

        try:
            loop = asyncio.get_running_loop()
            reason = getattr(request, "reason", "requires approval")
            params = getattr(request, "tool_params", {})

            # 用最简单的 stdout 输出确认提示，不依赖 Rich
            _sys.stdout.write("\n" + "=" * 50 + "\n")
            _sys.stdout.write(f"  需要授权: {request.tool_name}\n")
            _sys.stdout.write(f"  原因: {reason}\n")
            _sys.stdout.write(f"  参数: {params}\n")
            _sys.stdout.write("  允许吗? [y/n]: ")
            _sys.stdout.flush()

            def _ask():
                try:
                    return input("")
                except EOFError:
                    return "n"

            answer = await loop.run_in_executor(None, _ask)
            _sys.stdout.write("\n")
            _sys.stdout.flush()
            return answer.strip().lower().startswith("y")
        finally:
            if pause_event is not None:
                pause_event.set()
            if st is not None:
                st.start()

    return _confirm


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
    run_parser.add_argument(
        "--memory-backend",
        default="chromadb",
        choices=["chromadb", "qdrant"],
        help="Semantic memory backend (default: chromadb)",
    )
    run_parser.add_argument(
        "--enable-external-tools",
        action="store_true",
        default=False,
        help="Enable external tools (HTTP, DB, Browser) — disabled by default",
    )
    run_parser.add_argument(
        "--mcp-server",
        action="append",
        default=None,
        dest="mcp_servers",
        metavar="NAME:CMD_OR_URL",
        help=(
            "Connect to an MCP server.  Format: 'NAME:COMMAND_OR_URL'.  "
            "If the value after ':' starts with http:// or https:// it is "
            "treated as a streamable-HTTP URL; otherwise it is a stdio "
            "command (args space-separated).  Repeat for multiple servers."
        ),
    )

    sub.add_parser("version", help="Show version")

    serve_parser = sub.add_parser("serve", help="Start the HTTP API server")
    serve_parser.add_argument(
        "--port", "-p",
        type=int,
        default=8000,
        help="Port to listen on (default: 8000)",
    )
    serve_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)",
    )
    serve_parser.add_argument(
        "--memory-backend",
        default="chromadb",
        choices=["chromadb", "qdrant"],
        help="Semantic memory backend (default: chromadb)",
    )
    serve_parser.add_argument(
        "--enable-external-tools",
        action="store_true",
        default=False,
        help="Enable external tools (HTTP, DB, Browser) — disabled by default",
    )

    chat_parser = sub.add_parser("chat", help="Start an interactive chat session")
    chat_parser.add_argument(
        "--provider", "-p",
        default=None,
        choices=["anthropic", "openai", "deepseek", "google", "ollama"],
        help="LLM provider (overrides config)",
    )
    chat_parser.add_argument(
        "--model", "-m",
        default=None,
        help="Model name (overrides config)",
    )
    chat_parser.add_argument(
        "--mode",
        default=None,
        choices=["react", "plan_execute", "hierarchical"],
        help="Planning mode (overrides config)",
    )
    chat_parser.add_argument(
        "--memory-backend",
        default="chromadb",
        choices=["chromadb", "qdrant"],
        help="Semantic memory backend (default: chromadb)",
    )
    chat_parser.add_argument(
        "--enable-external-tools",
        action="store_true",
        default=False,
        help="Enable external tools (HTTP, DB, Browser)",
    )

    eval_parser = sub.add_parser("eval", help="Run a benchmark evaluation")
    eval_parser.add_argument(
        "benchmark",
        choices=["process_quality", "swebench"],
        help="Benchmark to run",
    )
    eval_parser.add_argument(
        "--provider", "-p",
        default="anthropic",
        choices=["anthropic", "openai", "deepseek", "google", "ollama"],
        help="LLM provider (default: anthropic)",
    )
    eval_parser.add_argument(
        "--model", "-m",
        default=None,
        help="Model name (overrides config)",
    )

    experiment_parser = sub.add_parser("experiment", help="Run an A/B experiment")
    experiment_parser.add_argument(
        "--name", "-n",
        required=True,
        help="Experiment name",
    )
    experiment_parser.add_argument(
        "--config-a",
        required=True,
        help="JSON string of variant A config, e.g. '{\"provider\":\"anthropic\"}'",
    )
    experiment_parser.add_argument(
        "--config-b",
        required=True,
        help="JSON string of variant B config",
    )
    experiment_parser.add_argument(
        "--task", "-t",
        default="Say hello.",
        help="Benchmark task description (default: 'Say hello.')",
    )
    experiment_parser.add_argument(
        "--runs",
        type=int,
        default=5,
        help="Number of runs per config (default: 5)",
    )

    args = parser.parse_args()

    if args.command == "version":
        from synapse import __version__
        print(f"Synapse v{__version__}")
        return

    if args.command == "run":
        task = " ".join(args.task)
        config = load_config()
        _check_api_key(config)

        from synapse.adapters.library import Synapse

        kwargs: dict[str, object] = {
            "provider": args.provider or "anthropic",
            "memory_backend": args.memory_backend,
            "enable_external_tools": args.enable_external_tools,
        }
        if args.model:
            kwargs["model"] = args.model
        if args.mode:
            kwargs["mode"] = args.mode

        mcp_servers = _parse_mcp_servers(args.mcp_servers)
        if mcp_servers is not None:
            kwargs["mcp_servers"] = mcp_servers

        synapse = Synapse(**kwargs)  # type: ignore[arg-type]

        async def _exec():
            print("Working...", flush=True)
            result = await synapse.run(task)
            return result

        result = asyncio.run(_exec())
        print(f"\n[Status: {result.status.value}]")
        print(result.output)
        return

    if args.command == "chat":
        config = load_config()
        if args.provider:
            config.provider.provider = args.provider
        if args.model:
            config.provider.model = args.model
        if args.mode:
            config.planning.mode = args.mode

        from synapse.adapters.library import Synapse

        # Mutable holder so the confirm callback can pause/resume the current spinner
        status_holder: list = []

        synapse = Synapse(
            provider=config.provider.provider,
            model=config.provider.model,
            config_path=None,
            memory_backend=args.memory_backend,
            enable_external_tools=args.enable_external_tools,
            confirm_callback=_make_confirm_callback(status_holder=status_holder),
        )

        try:
            from rich.console import Console
            from rich.markdown import Markdown
            from rich.status import Status
            console = Console()
            use_rich = True
        except ImportError:
            console = None
            use_rich = False

        async def _chat():
            from synapse.core.session import Session
            session = Session()
            welcome = (
                f"Synapse Chat [{config.provider.provider}/{config.provider.model}]"
            )
            if use_rich:
                console.print(f"[bold cyan]{welcome}[/bold cyan]")
                console.print("[dim]Type your task or question. /exit to quit, /clear to reset.[/dim]\n")
            else:
                print(welcome)
                print("Type your task or question. /exit to quit, /clear to reset.\n")

            while True:
                try:
                    if use_rich:
                        user_input = console.input("[bold green]> [/bold green]")
                    else:
                        user_input = input("> ")
                except (EOFError, KeyboardInterrupt):
                    print("\nGoodbye.")
                    break

                user_input = user_input.strip()
                if not user_input:
                    continue

                if user_input.lower() in ("/exit", "/quit"):
                    print("Goodbye.")
                    break

                if user_input.lower() == "/clear":
                    session = Session()
                    if use_rich:
                        console.print("[dim]Session cleared.[/dim]\n")
                    else:
                        print("Session cleared.\n")
                    continue

                if use_rich:
                    status = console.status("[dim]Working...[/dim]", spinner="dots")
                    status.start()

                    # Let confirm callback pause/resume this spinner
                    status_holder[:] = [status]

                    # Subscribe to tool call events to update spinner text
                    event_bus = synapse._container.resolve(EventBus)
                    if event_bus is not None:
                        async def _on_progress(event):
                            status.update(f"[dim]{event.message}[/dim]")

                        async def _on_tool_started(event):
                            status.update(f"[dim]Executing {event.tool_name}...[/dim]")

                        async def _on_tool_completed(event):
                            icon = "[OK]" if event.success else "[FAIL]"
                            status.update(f"[dim]Tool {event.tool_name} {icon} ({event.duration_ms}ms)[/dim]")

                        event_bus.subscribe("agent_progress", _on_progress)
                        event_bus.subscribe("tool_call_started", _on_tool_started)
                        event_bus.subscribe("tool_call_completed", _on_tool_completed)
                else:
                    print("Working...")

                try:
                    result = await synapse.run(user_input, session=session)
                except Exception as exc:
                    if use_rich:
                        status.stop()
                        console.print(f"[bold red]Error:[/bold red] {exc}")
                    else:
                        print(f"Error: {exc}")
                    continue
                finally:
                    if use_rich:
                        status.stop()
                        status_holder[:] = []  # clear holder
                        # Unsubscribe event handlers
                        event_bus.unsubscribe("agent_progress", _on_progress)
                        event_bus.unsubscribe("tool_call_started", _on_tool_started)
                        event_bus.unsubscribe("tool_call_completed", _on_tool_completed)

                if use_rich:
                    status_color = "green" if result.status.value == "success" else "yellow"
                    console.print(f"[dim]Status:[/dim] [{status_color}]{result.status.value}[/{status_color}]")
                    console.print(Markdown(result.output))
                    console.print()
                else:
                    print(f"\n[Status: {result.status.value}]")
                    print(result.output)
                    print()

        asyncio.run(_chat())
        return

    if args.command == "serve":
        import uvicorn

        from synapse.adapters.library import Synapse
        from synapse.adapters.server import create_app

        synapse = Synapse(
            provider="anthropic",
            memory_backend=args.memory_backend,
            enable_external_tools=args.enable_external_tools,
        )
        server_app = create_app(synapse_instance=synapse)
        uvicorn.run(server_app, host=args.host, port=args.port)
        return

    if args.command == "eval":
        asyncio.run(_run_eval(args))
        return

    if args.command == "experiment":
        asyncio.run(_run_experiment(args))
        return

    # No subcommand — launch main interface
    asyncio.run(_main_interface())


# ---- Main interface -------------------------------------------------------


def _show_welcome(config, use_rich, console):
    """Display welcome banner with project info."""
    from synapse import __version__
    cwd = str(Path.cwd())
    provider = config.provider.provider
    model = config.provider.model

    if use_rich:
        from rich.panel import Panel
        from rich.table import Table
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold cyan")
        grid.add_column(style="dim")
        grid.add_row("Version:", f"Synapse v{__version__}")
        grid.add_row("Provider:", f"{provider} / {model}")
        grid.add_row("Project:", cwd)
        grid.add_row("Tools:", "read, write, edit, glob, grep, shell, git")
        grid.add_row("Memory:", "session + project + user")
        console.print()
        console.print(Panel(grid, title="Synapse", border_style="cyan"))
        console.print("[dim]输入任务开始工作，输入 /help 查看命令[/dim]\n")
    else:
        print(f"\n  Synapse v{__version__} · {provider}/{model}")
        print(f"  {cwd}")
        print(f"  输入任务开始工作，输入 /help 查看命令\n")


def _show_help(console, use_rich):
    """Display available commands."""
    if use_rich:
        from rich.table import Table
        t = Table(title="可用命令")
        t.add_column("命令", style="bold green")
        t.add_column("说明")
        t.add_row("/help", "显示此帮助")
        t.add_row("/clear", "重置对话")
        t.add_row("/model <name>", "切换模型 (如 deepseek-chat)")
        t.add_row("/mode <name>", "切换规划模式 (react/plan_execute/hierarchical)")
        t.add_row("/tools", "列出可用工具")
        t.add_row("/exit, /quit", "退出")
        console.print()
        console.print(t)
    else:
        print("\n  命令: /help /clear /model /mode /tools /exit\n")


async def _main_interface():
    """Launch the main Synapse interface (synapse with no subcommand)."""
    config = load_config()
    provider = config.provider.provider
    model = config.provider.model

    try:
        from rich.console import Console
        console = Console()
        use_rich = True
    except ImportError:
        console = None
        use_rich = False

    _show_welcome(config, use_rich, console)

    from synapse.adapters.library import Synapse
    from synapse.core.session import Session

    synapse = Synapse(
        provider=provider, model=model, config_path=None,
        confirm_callback=_make_confirm_callback(),
    )
    session = Session()

    while True:
        try:
            if use_rich:
                user_input = console.input("[bold green]> [/bold green]")
            else:
                user_input = input("> ")
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        # ---- / 命令处理 ----
        if user_input.startswith("/"):
            parts = user_input.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            if cmd in ("/exit", "/quit"):
                print("Goodbye.")
                break
            elif cmd == "/help":
                _show_help(console, use_rich)
            elif cmd == "/clear":
                session = Session()
                if use_rich:
                    console.print("[dim]对话已重置[/dim]\n")
                else:
                    print("对话已重置\n")
            elif cmd == "/model" and arg:
                synapse = Synapse(
                    provider=provider, model=arg, config_path=None,
                    confirm_callback=_make_confirm_callback(),
                )
                model = arg
                if use_rich:
                    console.print(f"[dim]模型已切换为 {arg}[/dim]\n")
                else:
                    print(f"模型已切换为 {arg}\n")
            elif cmd == "/mode" and arg:
                try:
                    synapse = Synapse(
                        provider=provider, model=model, config_path=None,
                        confirm_callback=_make_confirm_callback(),
                        mode=arg,
                    )
                    if use_rich:
                        console.print(f"[dim]规划模式已切换为 {arg}[/dim]\n")
                    else:
                        print(f"规划模式已切换为 {arg}\n")
                except Exception as e:
                    if use_rich:
                        console.print(f"[red]切换失败: {e}[/red]\n")
                    else:
                        print(f"切换失败: {e}\n")
            elif cmd == "/tools":
                tools = ["read", "write", "edit", "glob", "grep", "shell", "git"]
                if use_rich:
                    console.print(f"[dim]可用工具: {', '.join(tools)}[/dim]\n")
                else:
                    print(f"可用工具: {', '.join(tools)}\n")
            else:
                if use_rich:
                    console.print(f"[red]未知命令: {cmd}，输入 /help 查看帮助[/red]\n")
                else:
                    print(f"未知命令: {cmd}，输入 /help 查看帮助\n")
            continue

        # ---- 普通任务 ----
        if use_rich:
            console.print("[dim]Working...[/dim]")
        else:
            print("Working...")

        try:
            result = await synapse.run(user_input, session=session)
        except Exception as exc:
            if use_rich:
                console.print(f"[bold red]错误:[/bold red] {exc}")
            else:
                print(f"错误: {exc}")
            continue

        if use_rich:
            from rich.markdown import Markdown
            color = "green" if result.status.value == "success" else "yellow"
            console.print(f"[dim]状态:[/dim] [{color}]{result.status.value}[/{color}]")
            console.print(Markdown(result.output))
            console.print()
        else:
            print(f"\n[状态: {result.status.value}]")
            print(result.output)
            print()


# ---- Eval command handler -------------------------------------------------


async def _run_eval(args) -> None:
    """Execute a named benchmark via the Synapse facade."""
    import json as _json

    from synapse.adapters.library import Synapse
    from synapse.eval.runner import BenchmarkRunner, Benchmark

    print(f"Benchmark: {args.benchmark}")
    print(f"Provider:  {args.provider}")

    # Build the benchmark
    if args.benchmark == "process_quality":
        from synapse.eval.benchmarks.process_bench import ProcessQualityBenchmark
        tasks = ProcessQualityBenchmark.tasks()
    elif args.benchmark == "swebench":
        from synapse.eval.benchmarks.swebench import SWEBenchAdapter
        adapter = SWEBenchAdapter()
        tasks = adapter.tasks()
    else:
        print(f"Unknown benchmark: {args.benchmark}")
        return

    benchmark = Benchmark(name=args.benchmark, tasks=tasks)
    print(f"Tasks:     {len(tasks)}")

    # Create Synapse instance
    kwargs: dict = {"provider": args.provider, "enable_eval": True}
    if args.model:
        kwargs["model"] = args.model

    synapse = Synapse(**kwargs)
    runner = BenchmarkRunner()
    result = await runner.run(benchmark, synapse.run)

    print(f"\n--- Results ---")
    print(f"Total:     {result.total}")
    print(f"Completed: {result.completed}")
    print(f"Failed:    {result.failed}")
    print(f"Duration:  {result.duration_ms}ms")

    for tr in result.results:
        status_icon = "+" if tr.status == "success" else "!" if tr.status == "failed" else "?"
        print(f"  [{status_icon}] {tr.task_id}: {tr.status} ({tr.duration_ms}ms)")


# ---- Experiment command handler -------------------------------------------


async def _run_experiment(args) -> None:
    """Execute an A/B experiment."""
    import json as _json

    from synapse.eval.experiments import Experiment

    config_a = _json.loads(args.config_a)
    config_b = _json.loads(args.config_b)

    print(f"Experiment: {args.name}")
    print(f"Config A:   {_json.dumps(config_a)}")
    print(f"Config B:   {_json.dumps(config_b)}")
    print(f"Task:       {args.task}")
    print(f"Runs:       {args.runs}")
    print()

    from synapse.adapters.library import Synapse

    async def benchmark(config: dict) -> float:
        synapse = Synapse(**config)
        result = await synapse.run(args.task)
        return float(result.metrics.duration_ms)

    import uuid
    experiment = Experiment(
        id=str(uuid.uuid4()),
        name=args.name,
        variables={"task": args.task},
        agent_config_a=config_a,
        agent_config_b=config_b,
        benchmark=benchmark,
        runs_per_config=args.runs,
    )

    print("Running experiment...")
    result = await experiment.run()

    print(f"\n--- Results ---")
    print(f"Config A metrics: {result.metrics_a}")
    print(f"Config B metrics: {result.metrics_b}")
    print(f"p-value:          {result.p_value}")
    print(f"Winner:           {result.winner or 'none (not significant)'}")


if __name__ == "__main__":
    main()
