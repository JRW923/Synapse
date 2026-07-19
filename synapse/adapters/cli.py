"""CLI entry point for Synapse."""

import argparse
import asyncio
import os as _os
import sys
import time as _time
from pathlib import Path

# ── Two-step Ctrl+C (double-press to exit) ───────────────────────────
# Python's ``signal.signal(SIGINT, ...)`` on Windows is unreliable
# because ``GenerateConsoleCtrlEvent`` / ``os.kill`` can terminate the
# process before the handler fires.  We register a console control
# handler ourselves via ``SetConsoleCtrlHandler``, which is called
# *before* Python and *before* the OS default handler.  Returning TRUE
# suppresses the "Terminate batch job (Y/N)?" prompt entirely.
_last_ctrl_c: float = 0.0
_ctrl_c_pressed: bool = False  # set by Ctrl+C handler, cleared by main loop

if sys.platform == "win32":
    import ctypes as _ctypes

    _HANDLER = _ctypes.WINFUNCTYPE(_ctypes.c_int, _ctypes.c_uint)

    @_HANDLER
    def _win_ctrl_handler(ctrl_type: int) -> int:
        global _last_ctrl_c, _ctrl_c_pressed
        if ctrl_type != 0:  # CTRL_C_EVENT
            return 0
        now = _time.monotonic()
        if now - _last_ctrl_c < 3.0:
            _ctypes.windll.kernel32.ExitProcess(0)
        _last_ctrl_c = now
        _ctrl_c_pressed = True
        _os.write(2, b"\n  Press Ctrl+C again to exit.\n")
        return 1  # TRUE — suppress OS prompt.

    _ctypes.windll.kernel32.SetConsoleCtrlHandler(_win_ctrl_handler, 1)
else:
    # Unix (Linux / macOS): use signal.SIGINT for double-tap exit.
    import signal as _signal

    def _unix_sigint_handler(_signum: int, _frame: object) -> None:
        global _last_ctrl_c, _ctrl_c_pressed
        now = _time.monotonic()
        if now - _last_ctrl_c < 3.0:
            _os._exit(0)
        _last_ctrl_c = now
        _ctrl_c_pressed = True
        _os.write(2, b"\n  Press Ctrl+C again to exit.\n")

    _signal.signal(_signal.SIGINT, _unix_sigint_handler)

from synapse.config import load_config
from synapse.config.schema import _effective_api_key
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
    from synapse.modules.providers.anthropic import AnthropicProvider

    try:
        from synapse.modules.providers.openai import OpenAIProvider  # noqa: F811
    except ImportError:
        OpenAIProvider = None  # type: ignore[assignment]

    try:
        from synapse.modules.providers.deepseek import DeepSeekProvider
    except ImportError:
        DeepSeekProvider = None  # type: ignore[assignment]

    try:
        from synapse.modules.providers.google import GoogleProvider
    except ImportError:
        GoogleProvider = None  # type: ignore[assignment]

    try:
        from synapse.modules.providers.ollama import OllamaProvider
    except ImportError:
        OllamaProvider = None  # type: ignore[assignment]

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
    from synapse.modules.planning.react import ReActPlanner
    from synapse.modules.planning.plan_execute import PlanExecutePlanner
    from synapse.modules.planning.hierarchical import HierarchicalPlanner

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

    # Context budget management
    partitioner = ContextPartitioner()
    compactor = ContextCompactor()
    c.register(ContextPartitioner, partitioner)
    c.register(ContextCompactor, compactor)

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


def _make_confirm_callback(pause_event=None, status_holder=None, exiting=None):
    """Return an async callback that prompts the user for tool-call approval.

    Displays three options: ``[A]llow`` / ``[D]eny`` / ``[Y]es to all``.
    *Yes to all* permanently allows future calls for the same tool name.
    """
    import sys as _sys

    _auto_allowed: set[str] = set()

    async def _confirm(request):
        tool_name = getattr(request, "tool_name", "unknown")

        # Permanent allow list.
        if tool_name in _auto_allowed:
            return True

        # Shutting down — deny without prompt.
        if exiting is not None and len(exiting) > 0 and exiting[0]:
            return False

        st = None
        if status_holder is not None and len(status_holder) > 0:
            st = status_holder[0]
            if st is not None:
                st.stop()

        if pause_event is not None:
            pause_event.clear()

        try:
            loop = asyncio.get_running_loop()

            _sys.stdout.write(f"\n  Auth: {tool_name}  [A]llow / [D]eny / [Y]es to all: ")
            _sys.stdout.flush()

            def _ask():
                try:
                    return input("").strip().lower()
                except EOFError:
                    return "d"

            answer = await loop.run_in_executor(None, _ask)

            if answer == "y":
                _auto_allowed.add(tool_name)
                return True
            if answer in ("a", ""):
                return True
            return False
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
    parser.add_argument(
        "--config", "-c",
        default=None,
        metavar="PATH",
        help="Path to synapse.yaml (default: auto-detect from CWD upward, then ~/.synapse/)",
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

    setup_parser = sub.add_parser("setup", help="Install launcher scripts for clean Ctrl+C")
    setup_parser.add_argument(
        "--dir",
        default=None,
        metavar="PATH",
        help="Directory for launcher scripts (default: ~/.local/bin)",
    )

    args = parser.parse_args()

    if args.command == "version":
        from synapse import __version__
        print(f"Synapse v{__version__}")
        return

    if args.command == "run":
        task = " ".join(args.task)
        config, _ = load_config()
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

        try:
            result = asyncio.run(_exec())
        except KeyboardInterrupt:
            return
        print(f"\n[Status: {result.status.value}]")
        print(result.output)
        return

    if args.command == "chat":
        config, _ = load_config()
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

        try:
            asyncio.run(_chat())
        except KeyboardInterrupt:
            pass
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
        try:
            asyncio.run(_run_eval(args))
        except KeyboardInterrupt:
            pass
        return

    if args.command == "experiment":
        try:
            asyncio.run(_run_experiment(args))
        except KeyboardInterrupt:
            pass
        return

    if args.command == "setup":
        _run_setup(args)
        return

    # No subcommand — launch main interface
    try:
        asyncio.run(_main_interface(args.config))
    except KeyboardInterrupt:
        pass


# ---- Main interface -------------------------------------------------------


#: Synapse ASCII art — brain hemispheres with synaptic stem.
_WELCOME_ART = (
    r"         ,--..__..--,",
    r"       /    ..    .. \\",
    r"      /  ,'  ``  ',  \\",
    r"     (  (  o    o  )  )",
    r"      \  `.  ..  .'  /",
    r"       \    `--'    /",
    r"        `..______..'",
    r"           │    │",
    r"      ─────┘    └─────",
)

_WELCOME_NAME = "Synapse"
_WELCOME_SUBTITLE = "connecting ideas into code"
_WELCOME_STATUS = "ready"


def _middle(text: str, limit: int) -> str:
    """Truncate with ellipsis in the middle if too long."""
    text = str(text).replace("\n", " ")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    left = (limit - 3) // 2
    right = limit - 3 - left
    return text[:left] + "..." + text[-right:]


def _show_welcome(console, config, config_path: str = ""):
    """pico-style boxed welcome banner with Rich colour accents."""
    from synapse import __version__
    from rich.text import Text

    provider = config.provider.provider
    model = config.provider.model
    cwd = str(Path.cwd())

    # 宽度沿用 Console 创建时检测到的值（已在 _main_interface 用 OS API 设置）。
    width = max(getattr(console, "width", None) or 80, 40)
    inner = width - 4
    gap = 3
    left_w = (inner - gap) // 2
    right_w = inner - gap - left_w

    def _print_plain(text: str, **kwargs) -> None:
        console.print(text, **kwargs)

    def _b(char: str = "=") -> str:
        return f"+{char * (width - 2)}+"

    def _centered(body: str) -> str:
        """Center *body* in the box.  Leading/trailing whitespace is stripped
        so that the visible content is centred, not the raw string."""
        stripped = body.strip()
        if not stripped:
            return f"| {'':<{inner}} |"
        return f"| {_middle(stripped, inner).center(inner)} |"

    def _pair_texts(l_label: str, l_val: str, r_label: str, r_val: str):
        l_vis = f"{l_label:<9} {l_val}"
        r_vis = f"{r_label:<9} {r_val}"
        l_pad = _middle(l_vis, left_w).ljust(left_w)
        r_pad = _middle(r_vis, right_w).ljust(right_w)
        return l_pad, r_pad

    def _print_pair(l_label: str, l_val: str, r_label: str, r_val: str) -> None:
        """Two-column row.  Labels are bright-magenta; values use default style
        (forced via ``Text`` to prevent any bleed or Rich re-interpretation)."""
        l_pad, r_pad = _pair_texts(l_label, l_val, r_label, r_val)
        console.print(
            "| ",
            Text(l_pad[:9], style="bold bright_magenta"),
            Text(l_pad[9:], style="default"),
            " " * gap,
            Text(r_pad[:9], style="bold bright_magenta"),
            Text(r_pad[9:], style="default"),
            " |",
            sep="",
        )

    # ── render ────────────────────────────────────────────────────────
    _print_plain(_b("="), style="bright_black")
    for art_line in _WELCOME_ART:
        _print_plain(_centered(art_line), style="bright_cyan")
    # Name · subtitle · status on one line
    tagline_plain = f"{_WELCOME_NAME}  ·  {_WELCOME_SUBTITLE}  ·  {_WELCOME_STATUS}"
    tagline_body = _middle(tagline_plain, inner).center(inner)
    tagline_rich = (
        tagline_body
        .replace(_WELCOME_NAME, f"[bold bright_cyan]{_WELCOME_NAME}[/bold bright_cyan]")
        .replace(_WELCOME_SUBTITLE, f"[dim italic]{_WELCOME_SUBTITLE}[/dim italic]")
        .replace(_WELCOME_STATUS, f"[dim green]{_WELCOME_STATUS}[/dim green]")
    )
    _print_plain(f"| {tagline_rich} |")
    _print_plain(_b("-"), style="bright_black")
    _print_plain(f"| {'':<{inner}} |")

    # Workspace row
    ws_full = f"WORKSPACE  {cwd}"
    ws_body = _middle(ws_full, inner)
    console.print(
        "| ",
        Text(ws_body[:9], style="bold bright_magenta"),
        Text(ws_body[9:].ljust(inner - 9), style="default"),
        " |",
        sep="",
    )

    _print_pair("MODEL", model, "VERSION", f"v{__version__}")
    _print_pair("PROVIDER", provider, "PLANNING", config.planning.mode)
    if config_path:
        _print_plain(
            f"|  [dim]config  {config_path}[/dim]{' ' * (inner - len('config  ' + config_path))} |"
        )

    _print_plain(f"| {'':<{inner}} |")
    _print_plain(_centered("type /help for commands"), style="dim")
    _print_plain(_b("="), style="bright_black")


def _show_help(console):
    """Display available commands — pico style."""
    from rich.table import Table
    console.print()
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(style="bold bright_cyan")
    t.add_column(style="dim")
    t.add_row("/help", "Show this help")
    t.add_row("/memory", "View working memory")
    t.add_row("/session", "Show session path")
    t.add_row("/reset", "Clear session state")
    t.add_row("/model [name]", "Show or switch model")
    t.add_row("/provider [name]", "Show or switch provider (anthropic/openai/deepseek/google/ollama)")
    t.add_row("/mode [name]", "Show or switch planning mode (react / plan_execute / hierarchical)")
    t.add_row("/tools", "List available tools")
    t.add_row("/exit, /quit", "Exit")
    console.print(t)
    console.print()


def _available_models(config):
    """Return (available, unavailable) model entries based on API key presence.

    Checks, in order: the entry's own api_key, the env var for that provider,
    and finally ``config.provider.api_key`` (for entries of the same provider).
    """
    avail: list = []
    unavail: list = []
    main_key = config.provider.api_key
    main_provider = config.provider.provider
    for entry in config.provider.models:
        key = _effective_api_key(entry)
        # fall back to the main config's api_key for same-provider entries
        if not key and entry.provider == main_provider:
            key = main_key
        if key or entry.provider == "ollama":
            avail.append(entry)
        else:
            unavail.append(entry)
    return avail, unavail


# ---- First-run wizard -----------------------------------------------------


def _first_run_wizard(console, config) -> None:
    """Rich-powered interactive setup for first-time users."""
    from synapse.config.schema import _PROVIDER_ENV_VARS

    console.print()
    console.print("[bold bright_cyan]Welcome to Synapse![/bold bright_cyan]")
    console.print("[dim]No API key found. Let's configure your first model.[/dim]")
    console.print()

    # Show providers
    providers = sorted(_PROVIDER_ENV_VARS.keys())
    console.print("[bold]Available providers:[/bold]")
    for i, p in enumerate(providers, 1):
        env = _PROVIDER_ENV_VARS[p]
        hint = f"env: {env}" if env else "no key needed"
        console.print(f"  [bold bright_cyan]{i}.[/bold bright_cyan] [bright_magenta]{p}[/bright_magenta] [dim]({hint})[/dim]")

    while True:
        choice = console.input(f"\n  [bold]Pick one [1-{len(providers)}]:[/bold] ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(providers):
            provider = providers[int(choice) - 1]
            break
        console.print("[red]Invalid choice.[/red]")

    env_var = _PROVIDER_ENV_VARS.get(provider, "")
    if provider == "ollama":
        api_key = ""
        console.print(f"\n  [dim]Ollama runs locally — no API key needed.[/dim]")
    else:
        api_key = console.input(f"  [bold]API key for {provider} ({env_var}):[/bold] ").strip()
        if not api_key:
            console.print(f"  [dim]No key entered. Set {env_var} in your environment instead.[/dim]")

    # Pick first model for this provider
    default_model = "unknown"
    for entry in config.provider.models:
        if entry.provider == provider:
            default_model = entry.model
            break

    model = console.input(
        f"  [bold]Model name[/bold] [dim](default: {default_model})[/dim]: "
    ).strip()
    if not model:
        model = default_model

    # Write config
    out_dir = Path.home() / ".synapse"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "config.yaml"
    out_path.write_text(
        f"# Synapse config — auto-generated by first-run wizard\n"
        f"provider:\n"
        f"  provider: {provider}\n"
        f"  model: {model}\n"
        f"  api_key: \"{api_key}\"\n",
        encoding="utf-8",
    )
    console.print(f"\n  [green]Config written to {out_path}[/green]")
    console.print(f"  [dim]provider: {provider}, model: {model}[/dim]\n")


def _first_run_wizard_plain(config) -> None:
    """Plain-text setup wizard (no Rich)."""
    from synapse.config.schema import _PROVIDER_ENV_VARS

    print("\nWelcome to Synapse!")
    print("No API key found. Let's configure your first model.\n")

    providers = sorted(_PROVIDER_ENV_VARS.keys())
    print("Available providers:")
    for i, p in enumerate(providers, 1):
        env = _PROVIDER_ENV_VARS[p]
        hint = f"env: {env}" if env else "no key needed"
        print(f"  {i}. {p} ({hint})")

    while True:
        choice = input(f"\nPick one [1-{len(providers)}]: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(providers):
            provider = providers[int(choice) - 1]
            break
        print("Invalid choice.")

    if provider == "ollama":
        api_key = ""
        print("\nOllama runs locally — no API key needed.")
    else:
        env_var = _PROVIDER_ENV_VARS.get(provider, "")
        api_key = input(f"API key for {provider} ({env_var}): ").strip()
        if not api_key:
            print(f"No key entered. Set {env_var} in your environment instead.")

    default_model = "unknown"
    for entry in config.provider.models:
        if entry.provider == provider:
            default_model = entry.model
            break

    model = input(f"Model name (default: {default_model}): ").strip()
    if not model:
        model = default_model

    out_dir = Path.home() / ".synapse"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "config.yaml"
    out_path.write_text(
        f"# Synapse config — auto-generated by first-run wizard\n"
        f"provider:\n"
        f"  provider: {provider}\n"
        f"  model: {model}\n"
        f"  api_key: \"{api_key}\"\n",
        encoding="utf-8",
    )
    print(f"\nConfig written to {out_path}")
    print(f"provider: {provider}, model: {model}\n")


# ---- Setup command --------------------------------------------------------


def _run_setup(args) -> None:
    """Create launcher scripts that bypass pyenv .bat shims on Windows.

    ``synapse setup`` generates two files:

    * ``synapse.cmd`` — CMD launcher (Ctrl+C still shows prompt; CMD limitation)
    * ``synapse.ps1`` — PowerShell launcher (Ctrl+C works cleanly)

    On Unix, generates a plain shell script.
    """
    python_exe = sys.executable
    dest = Path(args.dir) if args.dir else Path.home() / ".local" / "bin"
    dest.mkdir(parents=True, exist_ok=True)

    if sys.platform == "win32":
        _write_launcher(dest / "synapse.cmd", (
            "@echo off\r\n"
            "REM Synapse launcher — bypasses pyenv .bat shims.\r\n"
            f"\"{python_exe}\" -m synapse %*\r\n"
        ))
        _write_launcher(dest / "synapse.ps1", (
            "# Synapse launcher for PowerShell.\r\n"
            f"& \"{python_exe}\" -m synapse @args\r\n"
        ))
    else:
        _write_launcher(dest / "synapse", (
            "#!/usr/bin/env bash\n"
            "# Synapse launcher.\n"
            f'exec "{python_exe}" -m synapse "$@"\n'
        ), executable=True)

    print(f"Launchers written to {dest}")
    print()
    if sys.platform == "win32":
        print("Next steps:")
        print(f"  1. Add to user PATH (restart after):")
        print(f'     [Environment]::SetEnvironmentVariable(')
        print(f'         "PATH", "{dest};" +')
        print(f'         [Environment]::GetEnvironmentVariable("PATH","User"),')
        print(f'         "User")')
        print(f"  2. PowerShell alias (restart shell after):")
        print(f'     New-Item -ItemType Directory -Force (Split-Path $PROFILE)')
        print(f'     Add-Content $PROFILE \\"function synapse {{ & \\"{python_exe}\\" -m synapse @args }}\\"')
        print(f"  3. Use 'synapse' from PowerShell for clean Ctrl+C handling")


def _write_launcher(path: Path, content: str, executable: bool = False) -> None:
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(0o755)


async def _main_interface(config_path: str | None = None):
    """Launch the main Synapse interface (synapse with no subcommand)."""
    global _ctrl_c_pressed
    config, config_source = load_config(config_path)
    provider = config.provider.provider
    model = config.provider.model

    try:
        from rich.console import Console
        import shutil as _shutil
        _cols = _shutil.get_terminal_size((80, 24)).columns
        try:
            import os as _os
            _cols = _os.get_terminal_size().columns
        except Exception:
            pass
        console = Console(force_terminal=True, width=_cols)
        use_rich = True
    except ImportError:
        console = None
        use_rich = False

    if not use_rich:
        print(f"Synapse v0.1.0 · {provider}/{model}")

    from synapse.core.session import Session

    # First-run wizard: if no API key is configured anywhere, help the user set one.
    avail, _ = _available_models(config)
    if not avail:
        if use_rich:
            _first_run_wizard(console, config)
        else:
            _first_run_wizard_plain(config)
        # Reload config after wizard writes it.
        config, config_source = load_config(config_path)
        provider = config.provider.provider
        model = config.provider.model

    # Mutable holders shared with the confirm callback.
    status_holder: list = []
    exiting: list = [False]

    # Show the welcome banner immediately, before heavy imports.
    if use_rich:
        _show_welcome(console, config, config_source)
    else:
        print(f"输入任务开始工作，输入 /help 查看命令\n")

    # Deferred — created on first user input.
    _synapse: object = None
    session = Session()
    last_status = ""

    def _get_synapse():
        """Create (or return) the Synapse instance lazily."""
        nonlocal _synapse
        if _synapse is None:
            from synapse.adapters.library import Synapse as _Synapse
            _synapse = _Synapse(
                provider=provider,
                model=model,
                mode=config.planning.mode,
                config_path=None,
                confirm_callback=_make_confirm_callback(
                    status_holder=status_holder, exiting=exiting,
                ),
            )
        return _synapse

    while True:
        try:
            if use_rich:
                user_input = console.input("  [bold bright_cyan]synapse>[/bold bright_cyan] ")
            else:
                user_input = input("synapse> ")
        except EOFError:
            # Ctrl+C may cause a spurious EOF on some console hosts.
            if _ctrl_c_pressed:
                _ctrl_c_pressed = False
                continue
            exiting[0] = True
            break
        except KeyboardInterrupt:
            # KeyboardInterrupt may still fire if Python's own handler runs
            # despite our SetConsoleCtrlHandler returning TRUE.
            if _ctrl_c_pressed:
                _ctrl_c_pressed = False
                continue
            exiting[0] = True
            break

        user_input = user_input.strip()
        # Ignore blank lines, but also check for Ctrl+C flag (may produce
        # an empty string on some terminals after the handler fires).
        if not user_input:
            if _ctrl_c_pressed:
                _ctrl_c_pressed = False
            continue

        # ---- / 命令处理 ----
        if user_input.startswith("/"):
            parts = user_input.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            if cmd in ("/exit", "/quit"):
                exiting[0] = True
                break
            elif cmd == "/help":
                _show_help(console)
            elif cmd in ("/reset", "/clear"):
                session = Session()
                if use_rich:
                    console.print("[dim]Session cleared.[/dim]")
                else:
                    print("Session cleared.")
            elif cmd == "/memory":
                est = session.estimated_tokens if session.messages else 0
                budget = config.planning.max_tokens_per_task
                if use_rich:
                    console.print(f"[dim]Messages: {len(session.messages)}[/dim]")
                    console.print(f"[dim]Est. tokens: {est} / {budget}[/dim]")
                    console.print(f"[dim]Provider: {provider}/{model}[/dim]")
                    console.print(f"[dim]Workspace: {Path.cwd()}[/dim]")
                else:
                    print(f"Messages: {len(session.messages)}")
                    print(f"Est. tokens: {est} / {budget}")
                    print(f"Provider: {provider}/{model}")
                    print(f"Workspace: {Path.cwd()}")
            elif cmd == "/session":
                session_dir = Path.cwd() / ".synapse" / "sessions"
                if use_rich:
                    console.print(f"[dim]Session path: {session_dir}[/dim]")
                else:
                    print(f"Session path: {session_dir}")
            elif cmd == "/model":
                if arg:
                    # Switch model — must be in the available list.
                    avail, _ = _available_models(config)
                    candidates = [e for e in avail if e.model == arg or f"{e.provider}/{e.model}" == arg]
                    if not candidates:
                        if use_rich:
                            console.print(f"[red]'{arg}' is not available (no API key configured).[/red]")
                            console.print("[dim]Use /model alone to see available models.[/dim]")
                        else:
                            print(f"'{arg}' is not available (no API key configured). Use /model to list.")
                    else:
                        entry = candidates[0]
                        provider = entry.provider
                        model = entry.model
                        _synapse = None
                        prefix = f"[bright_cyan]>[/bright_cyan] [dim]{provider}/{model}[/dim]" if use_rich else f"{provider}/{model}"
                        if use_rich:
                            console.print(prefix)
                        else:
                            print(prefix)
                else:
                    # Show current + available models.
                    avail, unavail = _available_models(config)
                    if use_rich:
                        console.print(f"[bright_cyan]>[/bright_cyan] [bold]{provider}/{model}[/bold] [dim](current)[/dim]")
                        if avail:
                            avail_lines = [
                                f"  [green]{e.provider}/{e.model}[/green]"
                                for e in avail if not (e.provider == provider and e.model == model)
                            ]
                            if avail_lines:
                                console.print("[dim]Available:[/dim]")
                                for line in avail_lines:
                                    console.print(line)
                        if unavail:
                            console.print("[dim]Unconfigured (set API key to enable):[/dim]")
                            for e in unavail:
                                console.print(f"  [bright_black]{e.provider}/{e.model}[/bright_black]")
                    else:
                        print(f"Current: {provider}/{model}")
                        for e in avail:
                            print(f"  {e.provider}/{e.model}" if not (e.provider == provider and e.model == model) else f"* {e.provider}/{e.model}")
            elif cmd == "/provider":
                if not arg:
                    avail, _ = _available_models(config)
                    providers_set = sorted({e.provider for e in avail})
                    prefix = f"[bright_cyan]>[/bright_cyan] [dim]{provider}/{model} (current)[/dim]" if use_rich else f"{provider}/{model} (current)"
                    if use_rich:
                        console.print(prefix)
                        if providers_set:
                            console.print(f"[dim]Available providers: {', '.join(providers_set)}[/dim]")
                    else:
                        print(prefix)
                        if providers_set:
                            print(f"Available providers: {', '.join(providers_set)}")
                else:
                    new_provider = arg.lower()
                    avail, _ = _available_models(config)
                    provider_set = {e.provider for e in avail}
                    if new_provider not in provider_set:
                        if use_rich:
                            console.print(f"[red]'{new_provider}' is not available (no API key).[/red]")
                        else:
                            print(f"'{new_provider}' is not available (no API key).")
                    else:
                        provider = new_provider
                        # Pick the first model for this provider
                        for e in avail:
                            if e.provider == provider:
                                model = e.model
                                break
                        _synapse = None
                        prefix = f"[bright_cyan]>[/bright_cyan] [dim]{provider}/{model}[/dim]" if use_rich else f"{provider}/{model}"
                        if use_rich:
                            console.print(prefix)
                        else:
                            print(prefix)
            elif cmd == "/mode":
                if not arg:
                    prefix = f"[bright_cyan]>[/bright_cyan] [dim]Mode: {config.planning.mode}[/dim]" if use_rich else f"Mode: {config.planning.mode}"
                else:
                    config.planning.mode = arg
                    _synapse = None
                    prefix = f"[bright_cyan]>[/bright_cyan] [dim]Mode -> {arg}[/dim]" if use_rich else f"Mode -> {arg}"
                if use_rich:
                    console.print(prefix)
                else:
                    print(prefix)
            elif cmd == "/tools":
                tools = ["read", "write", "edit", "glob", "grep", "shell", "git"]
                msg = f"[bright_cyan]>[/bright_cyan] [dim]{', '.join(tools)}[/dim]" if use_rich else f"Tools: {', '.join(tools)}"
                if use_rich:
                    console.print(msg)
                else:
                    print(msg)
            else:
                console.print(f"[red]Unknown: {cmd}[/red]") if use_rich else print(f"Unknown: {cmd}")
            continue

        # ---- Task execution ----
        synapse = _get_synapse()

        if use_rich:
            from rich.status import Status
            status = console.status("[dim]Thinking...[/dim]", spinner="dots")
            status.start()
            status_holder[:] = [status]

            event_bus = synapse._container.resolve(EventBus)
            if event_bus is not None:
                async def _on_progress(event):
                    status.update(f"[dim]{event.message}[/dim]")
                async def _on_tool_started(event):
                    status.update(f"[dim]{event.tool_name} ...[/dim]")
                async def _on_tool_completed(event):
                    icon = "ok" if event.success else "FAIL"
                    status.update(f"[dim]{event.tool_name} [{icon}] ({event.duration_ms}ms)[/dim]")
                event_bus.subscribe("agent_progress", _on_progress)
                event_bus.subscribe("tool_call_started", _on_tool_started)
                event_bus.subscribe("tool_call_completed", _on_tool_completed)

        try:
            result = await synapse.run(user_input, session=session)
            last_status = result.status.value
        except asyncio.CancelledError:
            # Ctrl+C during task — let the outer KeyboardInterrupt handler deal
            # with it, just clean up spinner and exit the loop.
            exiting[0] = True
            break
        except Exception as exc:
            if use_rich:
                status.stop()
                console.print(f"  [bold red]{type(exc).__name__}:[/bold red] {exc}")
            else:
                print(f"Error: {exc}")
            continue
        finally:
            if use_rich:
                try:
                    status.stop()
                except Exception:
                    pass
                status_holder[:] = []
                if event_bus is not None:
                    try:
                        event_bus.unsubscribe("agent_progress", _on_progress)
                        event_bus.unsubscribe("tool_call_started", _on_tool_started)
                        event_bus.unsubscribe("tool_call_completed", _on_tool_completed)
                    except Exception:
                        pass

        if use_rich:
            from rich.markdown import Markdown
            console.print(Markdown(result.output))
        else:
            print(f"\n[Status: {result.status.value}]")
            print(result.output)


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
