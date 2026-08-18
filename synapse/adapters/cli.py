"""CLI entry point for Synapse."""

import argparse
import asyncio
import contextlib
import hashlib
import os as _os
import signal as _signal
import sys
import threading
import time as _time
from pathlib import Path

# Platform-specific key-input helpers for interactive selection
if sys.platform == "win32":
    import msvcrt as _msvcrt

    def _get_key() -> str:
        b = _msvcrt.getch()
        if b in (b"\xe0", b"\x00"):  # arrow-key prefix
            b2 = _msvcrt.getch()
            if b2 == b"H": return "up"
            if b2 == b"P": return "down"
            return ""
        try:
            return b.decode("utf-8").lower()
        except UnicodeDecodeError:
            return ""
else:
    import termios as _termios
    import tty as _tty

    def _get_key() -> str:
        fd = sys.stdin.fileno()
        old = _termios.tcgetattr(fd)
        try:
            _tty.setraw(fd)
            b = _os.read(fd, 3)
        finally:
            _termios.tcsetattr(fd, _termios.TCSADRAIN, old)
        if b == b"\x1b[A": return "up"
        if b == b"\x1b[B": return "down"
        if b in (b"\r", b"\n"): return "enter"
        if b == b"\x7f": return "backspace"
        try:
            return b.decode("utf-8").lower()
        except UnicodeDecodeError:
            return ""

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
        _os.write(2, "\n  再按一次 Ctrl+C 强制退出。\n".encode("utf-8"))
        # FALSE — let Python's SIGINT machinery run so the per-task handler
        # (request_cancel) fires. Returning TRUE swallowed the event, so a
        # single Ctrl+C never cancelled a task on Windows.
        return 0

    _ctypes.windll.kernel32.SetConsoleCtrlHandler(_win_ctrl_handler, 1)
else:
    # Unix (Linux / macOS): use signal.SIGINT for double-tap exit.
    def _unix_sigint_handler(_signum: int, _frame: object) -> None:
        global _last_ctrl_c, _ctrl_c_pressed
        now = _time.monotonic()
        if now - _last_ctrl_c < 3.0:
            _os._exit(0)
        _last_ctrl_c = now
        _ctrl_c_pressed = True
        _os.write(2, "\n  再按一次 Ctrl+C 退出。\n".encode("utf-8"))

    _signal.signal(_signal.SIGINT, _unix_sigint_handler)

from synapse.config import load_config, models_config_path
from synapse.config.models import apply_model_selection, set_default_model
from synapse.config.schema import _effective_api_key
from synapse.protocols.mcp import McpServerConfig
from synapse.core.exceptions import (
    SynapseError,
    ConfigError,
    ProviderError,
    ToolError,
    SandboxError,
    PlannerError,
)
from synapse.protocols.planner import Planner
from synapse.adapters.cli_render import (  # terminal rendering layer
    _BORDER, _BRAND, _HINT, _ICON, _INFO, _LABEL, _MASCOT_DARK,
    _MASCOT_RED, _MASCOT_TAIL, _MASCOT_YELLOW, _SUCCESS, _SYSTEM, _WARNING,
    _LiveRun, _cell_len, _clamp_text_by_cell, _format_token_count,
    _middle, _swallow,
)


def _install_cancel_handler(synapse, status_holder=None) -> object:
    """Install a SIGINT handler that requests cancellation on the active planner
    (instead of hard-killing the process) so a long task stops at the next
    iteration boundary. Returns the previous handler to restore afterwards.

    ponytail: only the ReAct/PlanExecute planners implement request_cancel();
    for others we fall back to the default handler (which raises
    KeyboardInterrupt, caught and saved by the caller).
    """
    planner = None
    try:
        planner = synapse._container.resolve(Planner)
    except Exception:
        planner = None
    prev = _signal.getsignal(_signal.SIGINT)

    def _handler(signum, frame):
        if status_holder is not None and status_holder:
            display = status_holder[0]
            if hasattr(display, "set_label"):
                display.set_label("正在取消...（等待当前步骤结束）")
        if planner is not None and hasattr(planner, "request_cancel"):
            planner.request_cancel()
        elif callable(prev):
            prev(signum, frame)

    try:
        _signal.signal(_signal.SIGINT, _handler)
    except ValueError:
        # Not in the main thread — can't install; rely on default behaviour.
        pass
    return prev


def _restore_cancel_handler(prev) -> None:
    try:
        _signal.signal(_signal.SIGINT, prev)
    except ValueError:
        pass


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

    from synapse.config.schema import _PROVIDER_ENV_VARS

    provider = config.provider.provider
    env_var = _PROVIDER_ENV_VARS.get(provider)

    if env_var:
        import os
        if os.environ.get(env_var):
            return  # found in env

        print(f"\n  No API key found for '{provider}'.\n")
        print(f"  Set it with one of:\n")
        print(f"    1. Environment:  set {env_var}=sk-your-key    (Windows CMD)")
        print(f"                     $env:{env_var} = \"sk-...\"    (PowerShell)")
        print(f"    2. Interactive:  run synapse and use /model add")
        print(f"    3. User config:  add the model to ~/.synapse/models.json")
        print()


def _make_confirm_callback(pause_event=None, status_holder=None, exiting=None, auth=None):
    """Return an async callback that prompts the user for tool-call approval.

    Displays three options: ``[A]llow`` / ``[D]eny`` / ``[Y]es to all``.
    *Yes to all* permanently allows future calls with the same approval
    signature (command first-token / parent dir / tool name) — both in this
    callback and in the shared ActionAuthorizer when *auth* is provided.
    """
    import sys as _sys

    _auto_allowed: set[str] = set()
    # Serialize prompts so concurrent swarm workers can't interleave reads on
    # the shared stdin (they all share this one callback instance).
    _prompt_lock = asyncio.Lock()

    def _signature(request) -> str:
        a = getattr(_confirm, "auth", None) or auth
        if a is not None:
            return a.approval_signature(request)
        return getattr(request, "tool_name", "unknown")

    def _describe(request) -> str:
        """Human-readable summary of the risky call: tool, risk level, target."""
        params = getattr(request, "tool_params", {}) or {}
        risk = getattr(request, "risk_level", "") or ""
        target = params.get("path") or params.get("command") or ""
        desc = f"{request.tool_name}"
        if risk:
            desc += f" [{risk}]"
        if target:
            desc += f"  →  {target}"
        return desc

    async def _confirm(request):
        tool_name = getattr(request, "tool_name", "unknown")
        sig = _signature(request)

        # Permanent allow list (session-scoped "yes to all" per signature).
        if sig in _auto_allowed:
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

            prompt = f"\n  [auth] {_describe(request)}  (a)llow / (d)eny / (y)es to all: "
            # One prompt at a time across all workers sharing this callback.
            async with _prompt_lock:
                _sys.stdout.write(prompt)
                _sys.stdout.flush()

                def _ask():
                    try:
                        return input("").strip().lower()
                    except EOFError:
                        return "d"

                answer = await loop.run_in_executor(None, _ask)

            if answer == "y":
                _auto_allowed.add(sig)
                a = getattr(_confirm, "auth", None) or auth
                if a is not None:
                    a.remember_approval(request)
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


async def _run_task_streamed(synapse, task, session, console, use_rich, status_holder=None):
    """Run *task* with a Rich live panel (REPL-style streaming).

    Shared by the REPL and the ``run`` subcommand so a one-shot task shows the
    same streamed LLM text + tool activity as an interactive session.  Without
    rich (or ``console is None``) it falls back to a plain run and lets the
    caller print the result.

    Exceptions are NOT swallowed here — the caller decides how to surface them
    (so the REPL keeps its Ctrl+C / error semantics, and ``run`` can print a
    friendly message instead of a raw traceback).
    """
    if not use_rich or console is None:
        return await synapse.run(task, session=session)
    live_run = _LiveRun(synapse, console, status_holder, persist_final=True)
    try:
        live_run.start()
        return await synapse.run(task, session=session)
    finally:
        live_run.stop()


def _resolve_session(resume):
    """Build the Session to use, honoring a ``--resume`` value.

    * ``None``       -> brand new session
    * ``"__latest__"`` -> most recently modified saved session
    * id / path      -> matched by id prefix, or loaded as a .json path
    """
    from pathlib import Path

    from synapse.core.session import Session

    if not resume:
        return Session()
    if resume == "__latest__":
        sessions = Session.list_sessions()
        if not sessions:
            print("No saved sessions found; starting a new one.")
            return Session()
        return sessions[0]
    p = Path(resume)
    if p.exists() and p.suffix == ".json":
        return Session.load(p)
    for s in Session.list_sessions():
        if s.id == resume or s.id.startswith(resume):
            return s
    print(f"No session matching '{resume}' found; starting a new one.")
    return Session()


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
    parser.add_argument(
        "--resume",
        nargs="?", const="__latest__", default=None, metavar="SESSION_ID",
        help="Resume a saved session by id (omit the value to resume the most "
             "recent session).",
    )
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Execute a task")
    run_parser.add_argument("task", nargs="+", help="Task description")
    run_parser.add_argument(
        "--provider", "-p",
        default=None,
        # No argparse choices here: custom providers registered in
        # ~/.synapse/models.json must be selectable too; _resolve_provider
        # owns the real validation (built-ins + custom_providers).
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
        choices=["react", "plan_execute", "hierarchical", "swarm"],
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
    run_parser.add_argument(
        "--yes", "-y",
        action="store_true",
        default=False,
        help="Auto-approve confirmation-required tool calls (write/execute) "
             "instead of denying them in non-interactive mode.",
    )
    run_parser.add_argument(
        "--resume",
        nargs="?", const="__latest__", default=None, metavar="SESSION_ID",
        help="Resume a saved session by id (omit the value to resume the most "
             "recent session). The task is appended to the existing history.",
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
    serve_parser.add_argument(
        "--connector-only",
        action="store_true",
        help="只提供本地 Connector 中继，不在服务器执行 Agent 或配置模型",
    )

    connect_parser = sub.add_parser(
        "connect", help="连接网页，在本机工作区执行任务",
    )
    connect_parser.add_argument(
        "--server",
        required=True,
        metavar="URL",
        help="网页服务地址，例如 https://agent.example.com",
    )
    connect_parser.add_argument(
        "--workspace",
        required=True,
        metavar="PATH",
        help="只允许网页任务操作的本机目录",
    )
    connect_parser.add_argument(
        "--name",
        default=None,
        metavar="NAME",
        help="网页显示的本地工作区名称（默认使用目录名）",
    )
    connect_parser.add_argument(
        "--pair",
        action="store_true",
        help="输入网页配对码（首次连接或重新绑定）",
    )

    web_parser = sub.add_parser(
        "web", help="单进程启动网页并自动连接本机工作区",
    )
    web_parser.add_argument(
        "--workspace",
        required=True,
        metavar="PATH",
        help="网页任务允许操作的本机目录（自动连接，无需配对码）",
    )
    web_parser.add_argument(
        "--port", "-p",
        type=int,
        default=8000,
        help="监听端口（默认：8000）",
    )
    web_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="绑定地址（默认：127.0.0.1，本机专用）",
    )
    web_parser.add_argument(
        "--name",
        default=None,
        metavar="NAME",
        help="网页显示的本地工作区名称（默认使用目录名）",
    )

    chat_parser = sub.add_parser("chat", help="Start an interactive chat session")
    chat_parser.add_argument(
        "--provider", "-p",
        default=None,
        # No argparse choices here: custom providers registered in
        # ~/.synapse/models.json must be selectable too; _resolve_provider
        # owns the real validation (built-ins + custom_providers).
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
        choices=["react", "plan_execute", "hierarchical", "swarm"],
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
    chat_parser.add_argument(
        "--resume",
        nargs="?", const="__latest__", default=None, metavar="SESSION_ID",
        help="Resume a saved session by id (omit the value to resume the most "
             "recent session).",
    )

    eval_parser = sub.add_parser("eval", help="Run a benchmark evaluation")
    eval_parser.add_argument(
        "benchmark",
        choices=["process_quality", "repo_pytest", "swebench", "terminal_smoke", "terminal_bench"],
        help="Benchmark to run",
    )
    eval_parser.add_argument(
        "--provider", "-p",
        default=None,
        # No argparse choices here: custom providers registered in
        # ~/.synapse/models.json must be selectable too; _resolve_provider
        # owns the real validation (built-ins + custom_providers).
        help="LLM provider (default: models.json selection)",
    )
    eval_parser.add_argument(
        "--model", "-m",
        default=None,
        help="Model name (overrides config)",
    )
    eval_parser.add_argument(
        "--dataset",
        default=None,
        help="Local benchmark JSON/JSONL dataset (required for swebench/terminal_bench)",
    )
    eval_parser.add_argument("--dataset-version", default=None, help="Dataset release/version")
    eval_parser.add_argument("--dataset-source", default=None, help="Dataset source URL or name")
    eval_parser.add_argument("--dataset-license", default=None, help="Dataset license identifier")
    eval_parser.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help="Run at most this many tasks",
    )
    eval_parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Repeat every benchmark task this many times",
    )
    eval_parser.add_argument(
        "--report",
        default=None,
        help="JSON report path (default: eval-results/<benchmark>-<timestamp>.json)",
    )
    eval_parser.add_argument(
        "--workspace",
        default=None,
        help="Explicit evaluation workspace (default: isolated temporary directory)",
    )
    eval_parser.add_argument(
        "--trusted-host-execution",
        action="store_true",
        help="允许可信评测数据在宿主机执行 grader 命令",
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
        help="Single diagnostic task (default: 'Say hello.')",
    )
    experiment_parser.add_argument(
        "--dataset",
        default=None,
        help="Terminal-Bench-compatible JSON/JSONL dataset for paired multi-task grading",
    )
    experiment_parser.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help="Maximum dataset tasks to run",
    )
    experiment_parser.add_argument(
        "--trusted-host-execution",
        action="store_true",
        help="允许可信数据集在宿主机执行 grader 命令",
    )
    experiment_parser.add_argument(
        "--runs",
        type=int,
        default=6,
        help="Number of paired runs per config (default: 6)",
    )
    experiment_parser.add_argument(
        "--primary-metric",
        default=None,
        choices=[
            "functional_success", "grader_score", "agent_reported_success",
            "duration_ms", "tokens", "tool_calls",
            "tool_success_rate", "safety_risk_attempts", "safety_policy_blocks",
            "safety_violations",
        ],
        help="Primary metric (default: functional_success for datasets, duration_ms otherwise)",
    )
    experiment_parser.add_argument(
        "--direction",
        choices=["higher", "lower"],
        default=None,
        help="Whether higher/lower is better (default: inferred from metric)",
    )
    experiment_parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Seed for paired run ordering and statistical resampling",
    )
    experiment_parser.add_argument(
        "--allowed-config-diff",
        action="append",
        default=None,
        metavar="PATH",
        help=(
            "Allowed effective config difference path; repeat as needed "
            "(e.g. runtime.eval_ablation.memory)"
        ),
    )
    experiment_parser.add_argument(
        "--report",
        default=None,
        help="JSON report path (default: eval-results/experiment-<name>-<timestamp>.json)",
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
        try:
            config, _ = load_config()
        except Exception as exc:
            print(_friendly_error(exc))
            return
        if not _ensure_first_model(config):
            return
        config, _ = load_config()
        if args.provider or args.model:
            apply_model_selection(
                config,
                args.provider or config.provider.provider,
                args.model or config.provider.model,
            )
        _check_api_key(config)

        from synapse.adapters.library import Synapse

        kwargs: dict[str, object] = {
            "memory_backend": args.memory_backend,
            "enable_external_tools": args.enable_external_tools,
        }
        if args.provider:
            kwargs["provider"] = args.provider
        if args.model:
            kwargs["model"] = args.model
        if args.mode:
            kwargs["mode"] = args.mode

        mcp_servers = _parse_mcp_servers(args.mcp_servers)
        if mcp_servers is not None:
            kwargs["mcp_servers"] = mcp_servers

        # L.3: non-interactive runs deny confirmation-required calls unless the
        # user explicitly opts in with --yes (auto-approve callback).
        if args.yes:
            async def _auto_approve(request):
                return True
            kwargs["confirm_callback"] = _auto_approve

        synapse = Synapse(**kwargs)  # type: ignore[arg-type]

        # Stream the run with a Rich live panel when available, so a one-shot
        # task shows the same progress as the REPL; fall back to plain output.
        try:
            from rich.console import Console
            console = Console()
            use_rich = bool(console.is_terminal)
        except ImportError:
            console = None
            use_rich = False

        session = _resolve_session(args.resume)
        from synapse.modules.todo import get_default_todo_store
        get_default_todo_store().bind_session(session.id)
        status_holder: list = []
        prev_handler = _install_cancel_handler(synapse, status_holder)
        try:
            result = asyncio.run(
                _run_task_streamed(synapse, task, session, console, use_rich, status_holder)
            )
        except KeyboardInterrupt:
            with _swallow("run: session save on interrupt"):
                session.save()
            print("任务已中断，当前会话已保存；可用 synapse --resume 继续。")
            return
        except Exception as exc:
            if use_rich:
                console.print(f"[bold red]{_friendly_error(exc)}[/bold red]")
            else:
                print(_friendly_error(exc))
            return
        finally:
            _restore_cancel_handler(prev_handler)

        _print_result(console, result, use_rich)
        session.save()
        return

    if args.command == "chat":
        # chat is the same REPL as the default interface — delegate rather than
        # maintain a second, degraded copy (no completion/history/commands).
        try:
            asyncio.run(_main_interface(
                args.config, getattr(args, "resume", None),
                getattr(args, "provider", None),
                getattr(args, "model", None),
                getattr(args, "mode", None),
            ))
        except KeyboardInterrupt:
            pass
        return

    if args.command == "serve":
        import uvicorn

        from synapse.adapters.server import create_app

        if args.connector_only:
            uvicorn.run(create_app(connector_only=True), host=args.host, port=args.port)
            return

        # 不强制要求 models.json：无配置时 Web UI 会在浏览器内提示填入用户自己的
        # API Key（POST /config），key 仅存于进程内存、不写服务器磁盘。若
        # ~/.synapse/models.json 已存在仍作为默认（无需在页面填）。
        # ponytail: 绑定 127.0.0.1 即本机，配合浏览器填 key 是本地优先用法；
        # 不要把它对公网开放（官方文档已声明不可匿名公开）。
        server_app = create_app()
        uvicorn.run(server_app, host=args.host, port=args.port)
        return

    if args.command == "connect":
        from synapse.adapters.connector import ConnectorError, run_connector

        try:
            asyncio.run(run_connector(
                server=args.server,
                workspace=args.workspace,
                name=args.name,
                config_path=args.config,
                pair=args.pair,
            ))
        except KeyboardInterrupt:
            print("已停止本地 Connector。")
        except ConnectorError as exc:
            print(f"Connector 启动失败：{exc}")
        return

    if args.command == "web":
        import webbrowser

        from synapse.adapters.connector import ConnectorError, run_connector
        from synapse.adapters.server import create_app

        app = create_app()
        broker = app.state.connector_broker
        # 自动建好配对并绑定本地 connector，省去手动配对码与第二个终端。
        pairing = broker.create_pairing()
        local_browser_token = pairing["browser_token"]

        host = args.host
        bind_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        server_url = f"http://{bind_host}:{args.port}"

        import uvicorn
        config = uvicorn.Config(
            app, host=host, port=args.port,
            log_level="warning", access_log=False,
        )
        server = uvicorn.Server(config)

        async def _web():
            # uvicorn 在独立线程运行，隔离其 SystemExit（如端口占用）与信号安装；
            # 主线程的 asyncio 循环只跑本地 connector。
            def _run_server():
                try:
                    asyncio.run(server.serve())
                except SystemExit:
                    pass  # 启动失败（端口占用等）已在主线程给出清晰提示
            server_thread = threading.Thread(target=_run_server, daemon=True)
            server_thread.start()
            # 等 uvicorn 真正开始监听后再让 connector 注册，否则首注册会失败。
            for _ in range(200):
                if getattr(server, "started", False):
                    break
                if not server_thread.is_alive():
                    break
                await asyncio.sleep(0.05)

            # 服务起不来（如端口被占用）时给出清晰错误，而非 uvicorn 堆栈。
            if not getattr(server, "started", False):
                print(f"Web 启动失败：服务未能在 {host}:{args.port} 启动"
                      f"（可能端口被占用或地址无效）")
                return

            print()
            print("  Synapse Web 已启动")
            print(f"  地址：  {server_url}")
            print(f"  工作区：{args.workspace}")
            print("  Ctrl+C 退出")
            print(flush=True)
            try:
                webbrowser.open(server_url)
            except Exception:
                pass

            def _on_registered(connection):
                app.state.local_connector = {
                    "connector_id": connection.connector_id,
                    "browser_token": local_browser_token,
                    "name": connection.name,
                }

            # 单 Ctrl+C 干净退出：关服务 + 取消 connector。
            loop = asyncio.get_running_loop()
            exiting: list[bool] = [False]
            connector_task = asyncio.create_task(run_connector(
                server=server_url,
                workspace=args.workspace,
                name=args.name,
                config_path=args.config,
                pair_code=pairing["pair_code"],
                on_registered=_on_registered,
            ))

            def _on_sigint():
                exiting[0] = True
                server.should_exit = True
                connector_task.cancel()

            try:
                loop.add_signal_handler(_signal.SIGINT, _on_sigint)
            except (NotImplementedError, ValueError):
                pass  # 不支持时回退到默认的两次 Ctrl+C 退出
            try:
                await connector_task
            except asyncio.CancelledError:
                pass
            finally:
                try:
                    loop.remove_signal_handler(_signal.SIGINT)
                except (NotImplementedError, ValueError):
                    pass
                server.should_exit = True
                # 被信号终止时直接退出：被取消的轮询线程会滞留，沿用全局
                # 处理器同款的 os._exit 让终端立即返回（should_exit 已让服务
                # 停止接收新连接，进程随即结束）。
                if exiting[0]:
                    _os._exit(0)
                server_thread.join(timeout=5)

        try:
            asyncio.run(_web())
        except KeyboardInterrupt:
            pass
        except ConnectorError as exc:
            print(f"Web 启动失败：{exc}")
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

    # No subcommand — launch main interface. provider/model/mode only exist on
    # the run/chat subparsers; absent here, so read them defensively.
    try:
        asyncio.run(_main_interface(
            args.config, getattr(args, "resume", None),
            getattr(args, "provider", None),
            getattr(args, "model", None),
            getattr(args, "mode", None),
        ))
    except KeyboardInterrupt:
        pass


# ---- Slash-command autocomplete ------------------------------------------

#: (command, description) shown in the completion menu, in display order.
#: Keep in sync with _show_help and the handlers in _main_interface.
_SLASH_COMMANDS: tuple = (
    ("/help",            "显示本帮助"),
    ("/memory",          "查看会话信息与 token 用量"),
    ("/session",         "显示会话路径"),
    ("/sessions",        "列出已保存的会话"),
    ("/resume",          "恢复会话（默认最近一次）"),
    ("/reset",           "清空会话"),
    ("/clear",           "/reset 的别名"),
    ("/model",           "显示/切换模型"),
    ("/model add",       "添加模型配置"),
    ("/provider",        "显示/切换供应商"),
    ("/mode",            "切换规划模式"),
    ("/tools",           "列出可用工具"),
    ("/context-report",  "上下文区块引用热力图"),
    ("/score",           "运行时评分 + 过程提示"),
    ("/todos",           "查看当前任务清单"),
    ("/checkpoint",      "为工作区打 git 快照"),
    ("/rewind",          "回滚到某个 checkpoint"),
    ("/exit",            "退出"),
    ("/quit",            "退出"),
)
_COMPLETION_LIMIT = 6  # max entries shown in the dropdown


def _make_prompt_session():
    """Build a prompt_toolkit session with slash-command completion, persistent
    history, and multi-line/paste support, or None if prompt_toolkit is absent."""
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
        from prompt_toolkit.key_binding.defaults import load_key_bindings
        from prompt_toolkit.styles import Style
    except ImportError:
        return None

    class _SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if not text.startswith("/"):
                return
            word = text.lstrip("/").lower()
            # Exact-prefix filter, preserve declared order.
            matches = [(c, d) for c, d in _SLASH_COMMANDS
                       if c.lstrip("/").startswith(word)]
            for cmd, desc in matches[:_COMPLETION_LIMIT]:
                yield Completion(
                    cmd,
                    start_position=-len(text),
                    display=cmd,
                    display_meta=desc,
                )

    # Persistent REPL history across sessions (~/.synapse/repl_history).
    history_dir = Path(_os.path.expanduser("~")) / ".synapse"
    history_dir.mkdir(parents=True, exist_ok=True)
    history = FileHistory(str(history_dir / "repl_history"))

    # Enter submits; Esc+Enter inserts a newline for multi-line input. Pasted
    # multi-line text is inserted verbatim (bracketed paste bypasses these
    # bindings), so it is kept as one block and submitted on Enter.
    # ponytail: key_processor picks the LAST matching binding (matches[-1]), so
    # the custom kb must come AFTER load_key_bindings() — with the order
    # reversed, the multiline `_newline` Enter binding won and Enter inserted a
    # newline instead of submitting. load_key_bindings() also supplies Tab
    # completion, history navigation and editing keys. `enable_history_search`
    # is deliberately off: it disables complete_while_typing (auto-completion).
    kb = KeyBindings()

    @kb.add("enter")
    def _(event):
        # Each keypress runs only the last matching binding, so no stop() is
        # needed — and KeyPressEvent has no stop() method (an old version of
        # this handler called event.stop() and every Enter crashed the app).
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _(event):
        event.current_buffer.insert_text("\n")

    try:
        return PromptSession(
            completer=_SlashCompleter(),
            history=history,
            multiline=True,
            key_bindings=merge_key_bindings([load_key_bindings(), kb]),
            complete_while_typing=True,
            bottom_toolbar=" Enter 发送 · Esc+Enter 换行 · Tab 补全 · ↑↓/Ctrl+R 历史 ",
            show_frame=True,
            style=Style.from_dict({
                "frame": "fg:ansibrightcyan",
                "prompt": "fg:ansibrightcyan bold",
            }),
        )
    except Exception:
        # No console (e.g. stdin piped, or a non-console host) — degrade to
        # plain input() instead of crashing startup.
        return None


# ---- Main interface -------------------------------------------------------


#: Terminal-native mascot. Each token becomes one full cell, so the image
#: stays aligned in PowerShell without requiring sixel/kitty image protocols.
_WELCOME_ART = (
    "    KKYYY       YYYKK    ",
    "   KYYYYY     YYYYYYK   ",
    "  KYYYYYYK   KYYYYYYK  ",
    " KYYYYYYYYK KYYYYYYYYK ",
    "KYYYYYYYYYYYYYYYYYYYYYK",
    "KYYYRRYYYYYYYYYYYRRYYYK",
    "KYYYYYYYYYYYYYYYYYYYYYK",
    " KYYYYYYYYYYYYYYYYYYYK ",
    "  KYYYYYYYYYYYYYYYYYK  ",
    "   KYYYYYYYYYYYYYYK   ",
    "    KYYYYYYYYYYK      ",
    "     KYYYYYYYK     BB ",
    "      KYYYYK     BBBBB",
)

_WELCOME_COMPACT_ART = (
    " KYYY   YYYK ",
    "KYYYYYYYYYYYK",
    "KYYRYYYYYRYYK",
    " KYYYYYYYYYK ",
    "  KYYYYYYK  ",
    "   KYYYK    ",
    "    K K  BB ",
)

_WELCOME_NAME = "Synapse"



def _result_summary(result) -> str:
    metrics = result.metrics
    total_tokens = metrics.tokens_input + metrics.tokens_output
    return (
        f"耗时 {metrics.duration_ms / 1000:.1f}s · token {_format_token_count(total_tokens)} · "
        f"工具 {metrics.tool_success_count}/{metrics.tool_call_count} 成功"
    )


def _result_hint(status: str) -> str:
    if status == "partial":
        return "可继续输入补充任务；一次性命令可用 synapse --resume 继续。"
    if status == "failed":
        return "建议检查 provider、API key 和最近工具错误，再缩小任务重试。"
    return ""


def _print_result(console, result, use_rich: bool) -> None:
    """Render a finished task result consistently across run/chat/REPL."""
    status = result.status.value
    summary = _result_summary(result)
    hint = _result_hint(status)
    if not use_rich or console is None:
        print(f"\n[Status: {status}]")
        print(result.output)
        print(summary)
        if hint:
            print(f"下一步：{hint}")
        return
    from rich.console import Group
    from rich import box
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.text import Text
    color = {"success": "green", "partial": "yellow", "failed": "red"}.get(
        result.status.value, "dim"
    )
    console.print()
    title = {"success": "TASK COMPLETE", "partial": "TASK PARTIAL", "failed": "TASK FAILED"}.get(
        status, "TASK FINISHED"
    )
    parts = [
        Text(f"● {title}", style=f"bold {color}"),
        Markdown(result.output or ""),
        Rule(style=_SYSTEM),
        Text(summary, style=_SYSTEM),
    ]
    if hint:
        parts.append(Text(f"下一步：{hint}", style=_SYSTEM))
    console.print(Panel(
        Group(*parts), border_style=color, box=box.ROUNDED,
        expand=True, padding=(0, 1),
    ))
    console.print()


def _show_welcome(console, config, config_path: str = "", session=None):
    """Render an aligned, width-aware workspace header."""
    from synapse import __version__
    from rich.text import Text

    provider = config.provider.provider
    model = config.provider.model
    cwd = str(Path.cwd())
    available, _ = _available_models(config)
    ready = any(e.provider == provider and e.model == model for e in available)
    status = "READY" if ready else "SETUP"
    status_style = _SUCCESS if ready else _WARNING
    session_label = "new"
    if session is not None:
        session_label = f"{session.id[:8]} · {len(session.messages)} msgs"

    # Rich ignores an explicit width on legacy Windows in ``console.width``;
    # ``_width`` is the constructor override and is otherwise None.
    width = max(
        getattr(console, "_width", None) or getattr(console, "width", None) or 80,
        40,
    )
    inner = width - 4
    tools_count = len(getattr(config.tools, "enabled", []) or [])
    config_label = str(config_path).replace(" + ", " · ") if config_path else "defaults"

    def _fit_text(content, limit: int) -> Text:
        if isinstance(content, Text):
            return _clamp_text_by_cell(content, max(0, limit))
        return Text(_middle(str(content), max(0, limit)))

    def _boxed_line(content="") -> Text:
        value = _fit_text(content, inner)
        line = Text("│ ", style=_BORDER)
        line.append(value)
        line.append(" " * max(0, inner - value.cell_len))
        line.append(" │", style=_BORDER)
        return line

    def _plain_line(content: str) -> Text:
        value = _middle(content, width)
        return Text(value + " " * max(0, width - _cell_len(value)))

    def _field(label: str, value: str, limit: int, style: str = _INFO) -> Text:
        text = Text()
        text.append("◆ ", style=_ICON)
        text.append(f"{label:<10}", style=_LABEL)
        text.append(_middle(value, max(0, limit - text.cell_len)), style=style)
        if text.cell_len < limit:
            text.append(" " * (limit - text.cell_len))
        return text

    def _mascot_line(art_line: str, limit: int) -> Text:
        styles = {
            "Y": _MASCOT_YELLOW,
            "R": _MASCOT_RED,
            "K": _MASCOT_DARK,
            "B": _MASCOT_TAIL,
        }
        text = Text()
        for token in art_line:
            if token in styles:
                text.append("█", style=styles[token])
            else:
                text.append(" ")
        return _clamp_text_by_cell(text, limit) if text.cell_len > limit else Text.assemble(
            text, " " * (limit - text.cell_len)
        )

    def _art_line(art_line: str, limit: int, row: int) -> Text:
        return _mascot_line(art_line, limit)

    if width < 52:
        console.print(_plain_line(f"◆ {_WELCOME_NAME}  {status}"))
        console.print(_plain_line(f"{provider}/{model} · {config.planning.mode}"))
        console.print(_plain_line(f"{_middle(cwd, width - 14)} · {session_label}"))
        return

    console.print(Text("╭" + "─" * (width - 2) + "╮", style=_BORDER))
    if width >= 92:
        logo_width = max(_cell_len(row) for row in _WELCOME_ART)
        gap = 3
        right_width = max(20, inner - logo_width - gap)
        rows = (
            Text.assemble(
                _art_line(_WELCOME_ART[0], logo_width, 0), " " * gap,
                _field("SYNAPSE", f"v{__version__}  ·  {status}", right_width, status_style),
            ),
            Text.assemble(
                _art_line(_WELCOME_ART[1], logo_width, 1), " " * gap,
                _field("WORKSPACE", cwd, right_width),
            ),
            Text.assemble(
                _art_line(_WELCOME_ART[2], logo_width, 2), " " * gap,
                _field("MODEL", f"{provider}/{model}", right_width),
            ),
            Text.assemble(
                _art_line(_WELCOME_ART[3], logo_width, 3), " " * gap,
                _field("PLANNING", config.planning.mode, right_width),
            ),
            Text.assemble(
                _art_line(_WELCOME_ART[4], logo_width, 4), " " * gap,
                _field("SESSION", session_label, right_width),
            ),
            Text.assemble(
                _art_line(_WELCOME_ART[5], logo_width, 5), " " * gap,
                _field("CONFIG", config_label, right_width, _HINT),
            ),
            Text.assemble(
                _art_line(_WELCOME_ART[6], logo_width, 6), " " * gap,
                _field("TOOLS", f"{tools_count} enabled", right_width, _HINT),
            ),
        )
        for row in rows:
            console.print(_boxed_line(row))
    else:
        logo_width = max(_cell_len(row) for row in _WELCOME_COMPACT_ART)
        gap = 3
        right_width = max(16, inner - logo_width - gap)
        rows = (
            Text.assemble(
                _art_line(_WELCOME_COMPACT_ART[0], logo_width, 0), " " * gap,
                _field("SYNAPSE", f"v{__version__}  ·  {status}", right_width, status_style),
            ),
            Text.assemble(
                _art_line(_WELCOME_COMPACT_ART[1], logo_width, 1), " " * gap,
                _field("WORKSPACE", cwd, right_width),
            ),
            Text.assemble(
                _art_line(_WELCOME_COMPACT_ART[2], logo_width, 2), " " * gap,
                _field("MODEL", f"{provider}/{model}", right_width),
            ),
            Text.assemble(
                _art_line(_WELCOME_COMPACT_ART[3], logo_width, 3), " " * gap,
                _field("PLANNING", config.planning.mode, right_width),
            ),
            Text.assemble(
                _art_line(_WELCOME_COMPACT_ART[4], logo_width, 4), " " * gap,
                _field("SESSION", session_label, right_width),
            ),
        )
        for row in rows:
            console.print(_boxed_line(row))
        tail_prefix = " " * (logo_width + gap)
        console.print(_boxed_line(Text.assemble(
            tail_prefix, _field("CONFIG", config_label, right_width, _HINT),
        )))
        console.print(_boxed_line(Text.assemble(
            tail_prefix, _field("TOOLS", f"{tools_count} enabled", right_width, _HINT),
        )))
    console.print(Text("╰" + "─" * (width - 2) + "╯", style=_BORDER))


def _input_frame(width: int, *, top: bool, rich: bool = True):
    """Build the input boundary so typed task text has an obvious home."""
    width = max(int(width or 80), 40)
    if top:
        label = " INPUT "
        remaining = max(0, width - 2 - len(label))
        raw = "╭" + "─" * (remaining // 2) + label + "─" * (remaining - remaining // 2) + "╮"
    else:
        raw = "╰" + "─" * (width - 2) + "╯"
    if not rich:
        return raw
    from rich.text import Text
    return Text(raw, style=_BORDER)


def _show_help(console):
    """Display available commands — pico style."""
    from rich.table import Table
    console.print()
    console.print(f"  [bold {_BRAND}]命令[/bold {_BRAND}]  [{_HINT}]输入命令，或直接描述任务[/{_HINT}]")
    t = Table(show_header=False, box=None, padding=(0, 2), pad_edge=False)
    t.add_column(style=f"bold {_BRAND}", no_wrap=True)
    t.add_column(style=_HINT)
    t.add_row("  /help", "显示本帮助")
    t.add_row("  /memory", "查看会话信息与 token 用量")
    t.add_row("  /session", "显示会话路径")
    t.add_row("  /sessions", "列出已保存的会话")
    t.add_row("  /resume [id]", "恢复会话（默认最近一次）")
    t.add_row("  /reset, /clear", "清空会话")
    t.add_row("  /model [name|num]", "显示/切换模型（切换会保存为默认）")
    t.add_row("  /model add", "添加模型配置")
    t.add_row("  /provider [name]", "显示/切换供应商")
    t.add_row("  /mode [name]", "规划模式 (react / plan_execute / hierarchical / swarm)")
    t.add_row("  /tools", "列出可用工具")
    t.add_row("  /context-report", "上下文区块引用 / 使用热力图")
    t.add_row("  /score", "运行时评分 (safety/process/quality/efficiency) + 提示")
    t.add_row("  /todos", "查看当前任务清单")
    t.add_row("  /checkpoint [label]", "为工作区打一个 git 快照（非 git 目录不可用）")
    t.add_row("  /rewind [num]", "回滚工作区到某个 checkpoint（无参数则列出）")
    t.add_row("  /exit, /quit", "退出")
    console.print(t)
    console.print()


def _format_citation_summary(synapse) -> str:
    """Phase 2 — one-line citation summary for the /memory command.

    Returns e.g. 'system 2/3 cited · core 1/5 cited · reference 0/2 cited'
    or empty string when no data is available.
    """
    if synapse is None:
        return ""
    try:
        report = synapse.get_citation_report()
    except Exception:
        return ""
    if not report:
        return ""
    blocks = report.get("blocks", [])
    if not blocks:
        return ""

    # Aggregate per zone.
    per_zone: dict[str, list[int]] = {}
    for row in blocks:
        z = row["zone"]
        per_zone.setdefault(z, [0, 0])  # [cited, used]
        per_zone[z][0] += row["cited"]
        per_zone[z][1] += row["usage"]

    parts = []
    for zone in ("system", "core", "reference", "overflow"):
        if zone in per_zone:
            cited, used = per_zone[zone]
            parts.append(f"{zone} {cited}/{used} cited")
    return " · ".join(parts) if parts else ""


def _show_context_report(console, synapse, use_rich: bool) -> None:
    """Phase 4 — render the citation/usage heatmap for the last task."""
    if synapse is None:
        msg = "Run a task first — no context to report yet."
        if use_rich:
            console.print(f"[dim]{msg}[/dim]")
        else:
            print(msg)
        return

    try:
        report = synapse.get_citation_report()
    except Exception as e:
        if use_rich:
            console.print(f"[red]Failed to build report: {e}[/red]")
        else:
            print(f"Failed to build report: {e}")
        return

    if not report:
        msg = "No citation data — run a task first."
        if use_rich:
            console.print(f"[dim]{msg}[/dim]")
        else:
            print(msg)
        return

    blocks = report.get("blocks", [])
    if not blocks:
        if use_rich:
            console.print("[dim]No context blocks in the last run.[/dim]")
        else:
            print("No context blocks in the last run.")
        return

    from rich.table import Table
    console.print()
    console.print(f"  [bold {_BRAND}]Context heatmap[/bold {_BRAND}]  [{_HINT}]citation rate = cited / used[/{_HINT}]")
    t = Table(show_header=True, box=None, padding=(0, 2), pad_edge=False)
    t.add_column("Zone", style=f"bold {_BRAND}")
    t.add_column("Source", style=_HINT)
    t.add_column("Pri", justify="right")
    t.add_column("Tokens", justify="right")
    t.add_column("Used", justify="right")
    t.add_column("Cited", justify="right")
    t.add_column("Rate", style="green")

    total_used = 0
    total_cited = 0
    for row in blocks:
        t.add_row(
            row["zone"],
            row["source"],
            str(row["priority"]),
            str(row["tokens"]),
            str(row["usage"]),
            str(row["cited"]),
            row["citation_rate"],
        )
        total_used += row["usage"]
        total_cited += row["cited"]

    console.print(t)
    if total_used > 0:
        rate = f"{total_cited}/{total_used} blocks cited"
        console.print(f"  [{_HINT}]Overall: {rate}[/{_HINT}]")
    console.print()


def _show_score(console, synapse, use_rich: bool) -> None:
    """L.4 — render the runtime score (safety/process/quality/efficiency) + hint."""
    if synapse is None:
        msg = "Run a task first — no score to show yet."
        (console.print(f"[dim]{msg}[/dim]") if use_rich else print(msg))
        return
    try:
        score = synapse.get_run_score()
    except Exception as e:
        (console.print(f"[red]Failed to read score: {e}[/red]") if use_rich
         else print(f"Failed to read score: {e}"))
        return
    if not score:
        msg = "No run score yet — run a task first."
        (console.print(f"[dim]{msg}[/dim]") if use_rich else print(msg))
        return

    def _fmt(d: dict) -> str:
        return "  ".join(f"{k}={v}" for k, v in d.items())

    header = f"{score.get('status', '')} · {score.get('task', '')[:60]}"
    if use_rich:
        console.print()
        console.print(f"  [bold {_BRAND}]Run score[/{_BRAND}]  [{_HINT}]{header}[/{_HINT}]")
        t = Table(show_header=False, box=None, padding=(0, 2), pad_edge=False)
        t.add_column(style=f"bold {_BRAND}", no_wrap=True)
        t.add_column(style=_HINT)
        for dim in ("safety", "process", "quality", "efficiency"):
            t.add_row(f"  {dim}", _fmt(score.get(dim) or {}))
        console.print(t)
        hint = score.get("process_hint")
        if hint:
            console.print(f"  [{_HINT}]hint:[/{_HINT}] {hint}")
        console.print()
    else:
        print(f"Run score — {header}")
        for dim in ("safety", "process", "quality", "efficiency"):
            print(f"  {dim}: {_fmt(score.get(dim) or {})}")
        if score.get("process_hint"):
            print(f"  hint: {score['process_hint']}")


def _show_todos(console, use_rich: bool) -> None:
    """s05 — render the current todo list (from the shared TodoStore)."""
    from synapse.modules.todo import get_default_todo_store
    todos = get_default_todo_store().list()
    if not todos:
        msg = "（当前没有待办）"
        (console.print(f"[dim]{msg}[/dim]") if use_rich else print(msg))
        return
    if use_rich:
        console.print()
        console.print(f"  [bold {_BRAND}]Todos[/{_BRAND}]")
        for t in todos:
            style = {"completed": "green", "in_progress": "yellow", "pending": "dim"}.get(t["status"], "dim")
            mark = {"completed": "x", "in_progress": ">", "pending": " "}.get(t["status"], " ")
            console.print(f"  [{style}][{mark}] {t['content']}[/{style}]")
        console.print()
    else:
        print("Todos:")
        for t in todos:
            mark = {"completed": "x", "in_progress": ">", "pending": " "}.get(t["status"], " ")
            print(f"  [{mark}] {t['content']}")


# L.5 — unified friendly error feedback: map SynapseError subclasses to a 中文
# "原因 + 建议动作" string so the CLI never dumps a raw traceback at the user.
_ERROR_GUIDE: dict[type[BaseException], tuple[str, str]] = {
    ConfigError: ("配置无效，启动即失败", "检查配置文件与环境变量（API key / provider / workspace_root 等），修正后重试。"),
    ProviderError: ("LLM 接口调用失败（限流 / 超时 / 鉴权）", "确认 API key 有效、网络通畅、账户未欠费或触发限流；稍后重试。"),
    ToolError: ("工具执行失败", "查看工具返回详情，确认参数与运行环境是否正确。"),
    SandboxError: ("操作被沙箱拦截", "目标路径或命令超出沙箱允许边界，请调整后再试。"),
    PlannerError: ("规划器失败（迭代上限 / 子任务死锁）", "尝试简化任务、切换规划模式（/mode），或分段下达指令。"),
}


def _friendly_error(exc: BaseException) -> str:
    """Translate an exception into a user-facing 中文 reason + suggested action.

    ``SynapseError`` subclasses get a dedicated entry from ``_ERROR_GUIDE``;
    everything else keeps a plain message (the traceback still stays hidden).
    """
    if isinstance(exc, SynapseError):
        reason, action = _ERROR_GUIDE.get(
            type(exc), ("Synapse 内部错误", "查看日志，必要时精简任务后重试。")
        )
        detail = f" — {exc}" if str(exc) else ""
        return f"原因：{reason}{detail}\n建议：{action}"
    return f"原因：{type(exc).__name__} — {exc}\n建议：若为偶发可重试；若持续，请检查输入或运行环境。"


def _available_models(config):
    """Return (available, unavailable) model entries based on API key presence.

    Merges built-in presets with user-defined custom providers.
    """
    from synapse.config.schema import ModelEntry
    avail: list = []
    unavail: list = []
    main_key = config.provider.api_key
    main_provider = config.provider.provider

    # Built-in presets
    for entry in config.provider.models:
        key = _effective_api_key(entry)
        if not key and entry.provider == main_provider:
            key = main_key
        # Ollama is local and intentionally has no API key.
        if key or entry.provider == "ollama":
            avail.append(entry)
        else:
            unavail.append(entry)

    # Custom providers → synthetic ModelEntry objects
    for cp in config.provider.custom_providers:
        from urllib.parse import urlparse

        key = cp.api_key
        host = (urlparse(cp.base_url).hostname or "").lower()
        keyless_local = host in {"localhost", "127.0.0.1", "::1"}
        for model_name in cp.models:
            entry = ModelEntry(provider=cp.name, model=model_name, api_key=cp.api_key, base_url=cp.base_url)
            if key or keyless_local:
                avail.append(entry)
            else:
                unavail.append(entry)

    return avail, unavail


def _pick_model(console, entries, initial: int = 0) -> int | None:
    """Interactive arrow-key selector using Rich Live display. Returns index or None."""
    n = len(entries)
    if n == 0:
        return None
    idx = max(0, min(initial, n - 1))
    from rich.text import Text
    from rich.live import Live

    def _render():
        lines = [Text("  Use arrow keys to move, Enter to select, Esc to cancel", style="dim")]
        for i, (label, _) in enumerate(entries):
            cursor = ">" if i == idx else " "
            line = Text.from_markup(f"  {cursor} {label}")
            if i == idx:
                line.stylize("bold bright_cyan")
            lines.append(line)
        return Text("\n").join(lines)

    console.print()  # blank line before picker
    with Live(_render(), console=console, refresh_per_second=30, transient=True) as live:
        while True:
            key = _get_key()
            if key == "up" and idx > 0:
                idx -= 1
                live.update(_render())
            elif key == "down" and idx < n - 1:
                idx += 1
                live.update(_render())
            elif key in ("enter", "\r", "\n", " "):
                return idx
            elif key == "\x1b":
                return None
            elif key.isdigit():
                num = int(key)
                if 1 <= num <= n:
                    return num - 1


# ---- First-run wizard -----------------------------------------------------


def _wizard_providers(config) -> list[str]:
    from synapse.config.schema import _PROVIDER_ENV_VARS

    return sorted(
        set(_PROVIDER_ENV_VARS) | {provider.name for provider in config.provider.custom_providers}
    )


def _has_stored_provider_key(config, provider: str) -> bool:
    return any(
        entry.provider == provider and bool(entry.api_key)
        for entry in config.provider.models
    ) or any(
        entry.name == provider and bool(entry.api_key)
        for entry in config.provider.custom_providers
    )


def _save_free_model(api_key) -> None:
    """Persist the OpenRouter free model as the persisted default."""
    from synapse.config.models import upsert_model
    from synapse.config.schema import OPENROUTER_BASE_URL, OPENROUTER_DEFAULT_MODEL

    upsert_model(
        "openrouter",
        OPENROUTER_DEFAULT_MODEL,
        api_key=api_key,
        base_url=OPENROUTER_BASE_URL,
        protocol="openai",
    )


def _first_run_wizard(console, config, *, first_run: bool = True) -> None:
    """Rich-powered model setup used by first run and ``/model add``."""
    from urllib.parse import urlparse

    from synapse.config.models import upsert_model
    from synapse.config.schema import _PROVIDER_ENV_VARS

    console.print()
    title = "欢迎使用 Synapse" if first_run else "添加模型"
    detail = "首次启动只需配置一次，之后将自动使用默认模型。" if first_run else "新配置会保存并设为默认模型。"
    console.print(f"  [bold {_BRAND}]{title}[/bold {_BRAND}]")
    console.print(f"  [{_HINT}]{detail}[/{_HINT}]\n")

    if first_run:
        from synapse.config.schema import OPENROUTER_DEFAULT_MODEL

        console.print(f"  [bold {_BRAND}]1.[/bold {_BRAND}] [{_LABEL}]使用免费模型[/{_LABEL}] [{_HINT}](OpenRouter，无需付费，推荐试用)[/{_HINT}]")
        console.print(f"  [bold {_BRAND}]2.[/bold {_BRAND}] [{_LABEL}]配置我自己的模型[/{_LABEL}]")
        while True:
            mode = console.input(f"\n  [bold]选择 [1-2]:[/bold] ").strip()
            if mode in {"1", "2"}:
                break
            console.print("  [red]请输入 1 或 2。[/red]")
        if mode == "1":
            env_var = _PROVIDER_ENV_VARS["openrouter"]
            console.print(f"\n  [{_HINT}]将使用 OpenRouter 免费模型 {OPENROUTER_DEFAULT_MODEL}。[/{_HINT}]")
            console.print(f"  [{_HINT}]需要 OpenRouter API Key（在 openrouter.ai 免费注册获取）。[/{_HINT}]")
            if _os.environ.get(env_var):
                api_key = None
                console.print(f"  [dim]已检测到环境变量 {env_var}，无需重复输入。[/dim]")
            else:
                while True:
                    api_key = console.input(f"  API key ({env_var}): ", password=True).strip()
                    if api_key:
                        break
                    console.print("  [red]API key 不能为空；也可以先设置对应环境变量。[/red]")
            _save_free_model(api_key)
            console.print(f"\n  [green]已保存到 {models_config_path()}[/green]")
            console.print(f"  [dim]默认模型：openrouter/{OPENROUTER_DEFAULT_MODEL}[/dim]\n")
            return
        console.print()

    providers = _wizard_providers(config)
    for i, name in enumerate(providers, 1):
        env = _PROVIDER_ENV_VARS.get(name, "")
        hint = f"env: {env}" if env else ("无需 key" if name == "ollama" else "已配置")
        console.print(
            f"  [bold {_BRAND}]{i}.[/bold {_BRAND}] [{_LABEL}]{name}[/{_LABEL}] "
            f"[{_HINT}]({hint})[/{_HINT}]"
        )
    console.print(f"  [bold {_BRAND}]0.[/bold {_BRAND}] [{_LABEL}]自定义兼容接口[/{_LABEL}]")

    custom = False
    while True:
        choice = console.input(
            f"\n  [bold]选择 provider [0-{len(providers)}，也可输入名称]:[/bold] "
        ).strip().lower()
        if choice.isdigit() and 1 <= int(choice) <= len(providers):
            provider = providers[int(choice) - 1]
            break
        if choice == "0":
            custom = True
            while True:
                provider = console.input("  [bold]Provider 名称:[/bold] ").strip().lower()
                if provider and all(ch.isalnum() or ch in "._-" for ch in provider):
                    break
                console.print("  [red]仅支持字母、数字、点、下划线和连字符。[/red]")
            break
        if choice in providers:
            provider = choice
            custom = provider not in _PROVIDER_ENV_VARS
            break
        console.print("  [red]无效选择。[/red]")

    base_url = ""
    protocol = "anthropic" if provider == "anthropic" else "openai"
    if custom:
        while True:
            base_url = console.input("  [bold]Base URL:[/bold] ").strip().rstrip("/")
            parsed = urlparse(base_url)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                break
            console.print("  [red]请输入完整的 http:// 或 https:// URL。[/red]")
        while True:
            protocol = console.input(
                "  [bold]API 协议[/bold] [dim](openai/anthropic，默认 openai)[/dim]: "
            ).strip().lower() or "openai"
            if protocol in {"openai", "anthropic"}:
                break
            console.print("  [red]仅支持 openai 或 anthropic。[/red]")

    env_var = _PROVIDER_ENV_VARS.get(provider, "")
    if provider == "ollama":
        api_key = ""
        console.print("  [dim]Ollama 在本地运行，无需 API key。[/dim]")
    elif _has_stored_provider_key(config, provider):
        api_key = None
        console.print("  [dim]将沿用该 provider 已保存的 API key。[/dim]")
    elif env_var and _os.environ.get(env_var):
        api_key = None
        console.print(f"  [dim]已检测到环境变量 {env_var}，无需重复输入。[/dim]")
    else:
        prompt = f"  API key ({env_var}): " if env_var else "  API key（本地无鉴权可留空）: "
        while True:
            api_key = console.input(prompt, password=True).strip()
            if api_key or custom:
                break
            console.print("  [red]API key 不能为空；也可以先设置对应环境变量。[/red]")

    while True:
        model = console.input("  [bold]Model ID[/bold]: ").strip()
        if model:
            break
        console.print("  [red]Model ID 不能为空。[/red]")

    upsert_model(
        provider,
        model,
        api_key=api_key,
        base_url=base_url,
        protocol=protocol,
    )
    console.print(f"\n  [green]已保存到 {models_config_path()}[/green]")
    console.print(f"  [dim]默认模型：{provider}/{model}[/dim]\n")


def _first_run_wizard_plain(config, *, first_run: bool = True) -> None:
    """Plain-text model setup used when Rich is unavailable."""
    import getpass
    from urllib.parse import urlparse

    from synapse.config.models import upsert_model
    from synapse.config.schema import _PROVIDER_ENV_VARS

    print("\n欢迎使用 Synapse" if first_run else "\n添加模型")
    print("首次启动只需配置一次，之后将自动使用默认模型。\n" if first_run else "新配置会保存并设为默认模型。\n")

    if first_run:
        from synapse.config.schema import OPENROUTER_DEFAULT_MODEL

        print("  1. 使用免费模型（OpenRouter，无需付费，推荐试用）")
        print("  2. 配置我自己的模型")
        while True:
            mode = input("\n选择 [1-2]: ").strip()
            if mode in {"1", "2"}:
                break
            print("请输入 1 或 2。")
        if mode == "1":
            env_var = _PROVIDER_ENV_VARS["openrouter"]
            print(f"\n将使用 OpenRouter 免费模型 {OPENROUTER_DEFAULT_MODEL}。")
            print(f"需要 OpenRouter API Key（在 openrouter.ai 免费注册获取）。")
            if _os.environ.get(env_var):
                api_key = None
                print(f"已检测到环境变量 {env_var}，无需重复输入。")
            else:
                while True:
                    api_key = getpass.getpass(f"API key ({env_var}): ").strip()
                    if api_key:
                        break
                    print("API key 不能为空；也可以先设置对应环境变量。")
            _save_free_model(api_key)
            print(f"\n已保存到 {models_config_path()}")
            print(f"默认模型：openrouter/{OPENROUTER_DEFAULT_MODEL}\n")
            return
        print()

    providers = _wizard_providers(config)
    for i, name in enumerate(providers, 1):
        env = _PROVIDER_ENV_VARS.get(name, "")
        hint = f"env: {env}" if env else ("无需 key" if name == "ollama" else "已配置")
        print(f"  {i}. {name} ({hint})")
    print("  0. 自定义兼容接口")

    custom = False
    while True:
        choice = input(f"\n选择 provider [0-{len(providers)}，也可输入名称]: ").strip().lower()
        if choice.isdigit() and 1 <= int(choice) <= len(providers):
            provider = providers[int(choice) - 1]
            break
        if choice == "0":
            custom = True
            while True:
                provider = input("Provider 名称: ").strip().lower()
                if provider and all(ch.isalnum() or ch in "._-" for ch in provider):
                    break
                print("仅支持字母、数字、点、下划线和连字符。")
            break
        if choice in providers:
            provider = choice
            custom = provider not in _PROVIDER_ENV_VARS
            break
        print("无效选择。")

    base_url = ""
    protocol = "anthropic" if provider == "anthropic" else "openai"
    if custom:
        while True:
            base_url = input("Base URL: ").strip().rstrip("/")
            parsed = urlparse(base_url)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                break
            print("请输入完整的 http:// 或 https:// URL。")
        while True:
            protocol = input("API 协议 (openai/anthropic，默认 openai): ").strip().lower() or "openai"
            if protocol in {"openai", "anthropic"}:
                break
            print("仅支持 openai 或 anthropic。")

    env_var = _PROVIDER_ENV_VARS.get(provider, "")
    if provider == "ollama":
        api_key = ""
        print("Ollama 在本地运行，无需 API key。")
    elif _has_stored_provider_key(config, provider):
        api_key = None
        print("将沿用该 provider 已保存的 API key。")
    elif env_var and _os.environ.get(env_var):
        api_key = None
        print(f"已检测到环境变量 {env_var}，无需重复输入。")
    else:
        prompt = f"API key ({env_var}): " if env_var else "API key（本地无鉴权可留空）: "
        while True:
            api_key = getpass.getpass(prompt).strip()
            if api_key or custom:
                break
            print("API key 不能为空；也可以先设置对应环境变量。")

    while True:
        model = input("Model ID: ").strip()
        if model:
            break
        print("Model ID 不能为空。")

    upsert_model(
        provider,
        model,
        api_key=api_key,
        base_url=base_url,
        protocol=protocol,
    )
    print(f"\n已保存到 {models_config_path()}")
    print(f"默认模型：{provider}/{model}\n")


def _ensure_first_model(config) -> bool:
    """Configure the first model for command modes without a Rich home screen."""
    if models_config_path().exists():
        return True
    if not sys.stdin.isatty():
        print(f"尚未配置模型。请先在交互终端运行 synapse，配置将保存到 {models_config_path()}。")
        return False
    try:
        _first_run_wizard_plain(config)
        return True
    except (EOFError, KeyboardInterrupt):
        print("已取消首次配置；未写入 models.json。")
        return False


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


async def _main_interface(config_path: str | None = None, resume: str | None = None,
                          provider: str | None = None, model: str | None = None,
                          mode: str | None = None):
    """Launch the main Synapse interface (synapse with no subcommand).

    ``chat`` delegates here (it is the same REPL, not a degraded copy).
    """
    global _ctrl_c_pressed
    try:
        config, config_source = load_config(config_path)
    except Exception as exc:
        print(_friendly_error(exc))
        return
    if provider:
        config.provider.provider = provider
    if model:
        config.provider.model = model
    if mode:
        config.planning.mode = mode
    provider = config.provider.provider
    model = config.provider.model

    try:
        from rich.console import Console
        console = Console()  # Rich only when stdout is an actual terminal.
        use_rich = bool(console.is_terminal)
    except ImportError:
        console = None
        use_rich = False

    if not use_rich:
        print(f"Synapse v0.1.0 · {provider}/{model}")

    from synapse.core.session import Session

    # models.json is the first-run marker. A legacy YAML key must not skip the
    # one-time model setup, otherwise later launches still lack a persisted default.
    first_run = not models_config_path().exists()
    avail, _ = _available_models(config)
    current_ready = any(
        e.provider == config.provider.provider and e.model == config.provider.model
        for e in avail
    )
    if first_run or not current_ready:
        if not sys.stdin.isatty():
            print(f"模型尚未就绪。请先在交互终端运行 synapse，配置将保存到 {models_config_path()}。")
            return
        try:
            if use_rich:
                _first_run_wizard(console, config, first_run=first_run)
            else:
                _first_run_wizard_plain(config, first_run=first_run)
        except (EOFError, KeyboardInterrupt):
            print("已取消首次配置；未写入 models.json。")
            return
        # Reload config after wizard writes it.
        try:
            config, config_source = load_config(config_path)
        except Exception as exc:
            print(_friendly_error(exc))
            return
        provider = config.provider.provider
        model = config.provider.model

    # Mutable holders shared with the confirm callback.
    status_holder: list = []
    exiting: list = [False]
    prompt_session = _make_prompt_session() if use_rich else None

    # Resolve the session before rendering the home screen so it can show a
    # useful resume hint without importing the heavy runtime.
    session = _resolve_session(resume)
    from synapse.modules.todo import get_default_todo_store
    get_default_todo_store().bind_session(session.id)

    # Show the welcome banner immediately, before heavy imports.
    if use_rich:
        _show_welcome(console, config, config_source, session)
        _last_cols = console.width
    else:
        print(f"输入任务开始工作，输入 /help 查看命令\n")

    # Deferred — created on first user input.
    _synapse: object = None
    last_status = ""

    def _activate_model(entry) -> bool:
        """Switch this session and persist the same model for the next launch."""
        nonlocal provider, model, _synapse
        try:
            set_default_model(entry.provider, entry.model)
        except Exception as exc:
            message = _friendly_error(exc)
            if use_rich:
                console.print(f"[red]{message}[/red]")
            else:
                print(message)
            return False
        provider, model = entry.provider, entry.model
        apply_model_selection(config, provider, model)
        _synapse = None
        return True

    def _get_synapse():
        """Create (or return) the Synapse instance lazily."""
        nonlocal _synapse
        if _synapse is None:
            from synapse.adapters.library import Synapse as _Synapse
            cb = _make_confirm_callback(
                status_holder=status_holder, exiting=exiting,
            )
            _synapse = _Synapse(
                provider=provider,
                model=model,
                mode=config.planning.mode,
                config_path=None,
                confirm_callback=cb,
            )
            # Late-bind the shared ActionAuthorizer so "yes to all" memory and
            # the callback stay in sync (the container only exists post-init).
            with _swallow("repl: bind authorizer"):
                from synapse.modules.security.auth import ActionAuthorizer
                cb.auth = _synapse._container.resolve(ActionAuthorizer)
        return _synapse

    while True:
        # Re-render banner if terminal resized (font zoom changes column count).
        if use_rich and console.width != _last_cols:
            _last_cols = console.width
            console.print()
            _show_welcome(console, config, config_source, session)

        input_frame_open = False
        try:
            if prompt_session is not None:
                from prompt_toolkit.formatted_text import HTML
                user_input = await prompt_session.prompt_async(
                    HTML('<ansicyan><b>◆ synapse › </b></ansicyan>'),
                    prompt_continuation=HTML('<ansicyan> </ansicyan>'),
                )
            elif use_rich:
                console.print(_input_frame(console.width, top=True))
                input_frame_open = True
                user_input = console.input(f"│ [bold {_BRAND}]◆ synapse › [/bold {_BRAND}]")
                console.print(_input_frame(console.width, top=False))
                input_frame_open = False
            else:
                print(_input_frame(80, top=True, rich=False))
                input_frame_open = True
                user_input = input("│ ◆ synapse › ")
                print(_input_frame(80, top=False, rich=False))
                input_frame_open = False
        except EOFError:
            if input_frame_open:
                (console.print(_input_frame(console.width, top=False)) if use_rich
                 else print(_input_frame(80, top=False, rich=False)))
            # Ctrl+C may cause a spurious EOF on some console hosts.
            if _ctrl_c_pressed:
                _ctrl_c_pressed = False
                continue
            exiting[0] = True
            break
        except KeyboardInterrupt:
            if input_frame_open:
                (console.print(_input_frame(console.width, top=False)) if use_rich
                 else print(_input_frame(80, top=False, rich=False)))
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
                from synapse.modules.todo import get_default_todo_store
                get_default_todo_store().bind_session(session.id)
                # SESSION memory outlives the Session object — clear it too so
                # prior tasks' summaries don't leak into the next task.
                if _synapse is not None:
                    with _swallow("/reset: clear session memory"):
                        getattr(_synapse, "clear_session_memory", lambda: None)()
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
                    # Phase 2 — show citation rate per zone from the last task.
                    citation_line = _format_citation_summary(_synapse)
                    if citation_line:
                        console.print(f"[{_HINT}]Context: {citation_line}[/{_HINT}]")
                else:
                    print(f"Messages: {len(session.messages)}")
                    print(f"Est. tokens: {est} / {budget}")
                    print(f"Provider: {provider}/{model}")
                    print(f"Workspace: {Path.cwd()}")
                    citation_line = _format_citation_summary(_synapse)
                    if citation_line:
                        print(f"Context: {citation_line}")
            elif cmd == "/session":
                from synapse.core.session import DEFAULT_SESSION_DIR
                path = DEFAULT_SESSION_DIR / f"{session.id}.json"
                if use_rich:
                    console.print(f"[dim]Session id: {session.id}[/dim]")
                    console.print(f"[dim]Saved at:   {path}[/dim]")
                    console.print(f"[dim]Messages:   {len(session.messages)}[/dim]")
                else:
                    print(f"Session id: {session.id}")
                    print(f"Saved at:   {path}")
                    print(f"Messages:   {len(session.messages)}")
            elif cmd == "/sessions":
                from synapse.core.session import Session as _S
                sessions = _S.list_sessions()
                msg = "No saved sessions." if not sessions else (
                    "Saved sessions:\n" + "\n".join(
                        f"  {s.id}  ({len(s.messages)} msgs)" for s in sessions[:10]
                    )
                )
                if use_rich:
                    console.print(f"[dim]{msg}[/dim]")
                else:
                    print(msg)
            elif cmd == "/resume":
                session = _resolve_session(arg or "__latest__")
                from synapse.modules.todo import get_default_todo_store
                get_default_todo_store().bind_session(session.id)
                note = f"Resumed session {session.id} ({len(session.messages)} msgs)."
                if use_rich:
                    console.print(f"[dim]{note}[/dim]")
                else:
                    print(note)
            elif cmd == "/context-report":
                _show_context_report(console, _synapse, use_rich)
            elif cmd == "/score":
                _show_score(console, _synapse, use_rich)
            elif cmd == "/todos":
                _show_todos(console, use_rich)
            elif cmd in ("/checkpoint", "/rewind"):
                from synapse.modules.checkpoint import CheckpointManager
                mgr = CheckpointManager(Path.cwd())
                if not mgr.available():
                    hint = "当前目录不是 git 仓库，checkpoint 不可用。"
                    if use_rich:
                        console.print(f"[yellow]{hint}[/yellow]")
                    else:
                        print(hint)
                elif cmd == "/checkpoint":
                    cp = mgr.create(label=arg or "manual")
                    msg = f"已创建 checkpoint {cp.label}" if cp else "checkpoint 创建失败"
                    if use_rich:
                        console.print(f"[dim]{msg}[/dim]")
                    else:
                        print(msg)
                else:  # /rewind
                    cps = mgr.list()
                    if not cps:
                        hint = "没有可用的 checkpoint。"
                        if use_rich:
                            console.print(f"[yellow]{hint}[/yellow]")
                        else:
                            print(hint)
                    elif arg.isdigit() and 1 <= int(arg) <= len(cps):
                        note = mgr.restore(cps[int(arg) - 1])
                        if use_rich:
                            console.print(f"[green]{note}[/green]")
                        else:
                            print(note)
                    elif arg:
                        hint = f"无效编号：{arg}（1-{len(cps)}）"
                        if use_rich:
                            console.print(f"[red]{hint}[/red]")
                        else:
                            print(hint)
                    else:
                        lines = "\n".join(
                            f"  {i}. {c.label}  ({c.timestamp})"
                            for i, c in enumerate(cps[-10:], start=len(cps) - min(10, len(cps)) + 1)
                        )
                        tail = "\n最新 10 条如上；用 /rewind <编号> 回滚（tracked 文件恢复，untracked 保留）。"
                        if use_rich:
                            console.print(f"[dim]Checkpoints:\n{lines}{tail}[/dim]")
                        else:
                            print(f"Checkpoints:\n{lines}{tail}")
            elif cmd == "/model":
                if arg.lower() == "add":
                    try:
                        if use_rich:
                            _first_run_wizard(console, config, first_run=False)
                        else:
                            _first_run_wizard_plain(config, first_run=False)
                        config, config_source = load_config(config_path)
                        provider = config.provider.provider
                        model = config.provider.model
                        _synapse = None
                    except (EOFError, KeyboardInterrupt):
                        if use_rich:
                            console.print("[dim]已取消添加模型。[/dim]")
                        else:
                            print("已取消添加模型。")
                    except Exception as exc:
                        message = _friendly_error(exc)
                        if use_rich:
                            console.print(f"[red]{message}[/red]")
                        else:
                            print(message)
                elif arg:
                    avail, _ = _available_models(config)
                    if arg.isdigit():
                        idx = int(arg) - 1
                        if 0 <= idx < len(avail):
                            entry = avail[idx]
                            if _activate_model(entry):
                                prefix = f"[bright_cyan]>[/bright_cyan] [dim]{provider}/{model} · 已设为默认[/dim]" if use_rich else f"{provider}/{model} · 已设为默认"
                                if use_rich: console.print(prefix)
                                else: print(prefix)
                        else:
                            if use_rich: console.print(f"[red]Invalid number (1-{len(avail)}).[/red]")
                            else: print(f"Invalid number (1-{len(avail)}).")
                    else:
                        candidates = [e for e in avail if e.model == arg or f"{e.provider}/{e.model}" == arg]
                        if candidates:
                            entry = candidates[0]
                            if _activate_model(entry):
                                prefix = f"[bright_cyan]>[/bright_cyan] [dim]{provider}/{model} · 已设为默认[/dim]" if use_rich else f"{provider}/{model} · 已设为默认"
                                if use_rich: console.print(prefix)
                                else: print(prefix)
                        else:
                            if use_rich: console.print(f"[red]'{arg}' is not available.[/red]")
                            else: print(f"'{arg}' is not available.")
                else:
                    avail, unavail = _available_models(config)
                    if not avail:
                        if use_rich: console.print("[red]No models available. Set an API key first.[/red]")
                        else: print("No models available. Set an API key first.")
                        continue
                    # Build entries for interactive picker
                    cur_idx = 0
                    pick_entries = []
                    for i, e in enumerate(avail):
                        label = f"{e.provider}/{e.model}"
                        if e.provider == provider and e.model == model:
                            label += " [dim](current)[/dim]"
                            cur_idx = i
                        pick_entries.append((label, (e.provider, e.model)))
                    if not use_rich:
                        for i, (label, _) in enumerate(pick_entries, 1):
                            print(f"  {i}. {label.replace(' [dim]', ' ').replace('[/dim]', '')}")
                        print("使用 /model <编号|名称> 切换，/model add 添加模型。")
                        continue
                    idx = _pick_model(console, pick_entries, initial=cur_idx)
                    if idx is not None:
                        entry = avail[idx]
                        if _activate_model(entry):
                            n_msgs = len(session.messages)
                            if n_msgs:
                                hint = f"[dim]Session preserved ({n_msgs} messages).[/dim]"
                                if use_rich: console.print(hint)
                                else: print(f"Session preserved ({n_msgs} messages).")
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
                        # Pick the first model for this provider
                        for e in avail:
                            if e.provider == new_provider:
                                entry = e
                                break
                        if _activate_model(entry):
                            prefix = f"[bright_cyan]>[/bright_cyan] [dim]{provider}/{model} · 已设为默认[/dim]" if use_rich else f"{provider}/{model} · 已设为默认"
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
                tools = ["read", "write", "edit", "glob", "grep", "shell", "git", "web_search"]
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

        live_run = _LiveRun(synapse, console, status_holder) if use_rich else None
        if live_run is not None:
            live_run.start()

        prev_handler = _install_cancel_handler(synapse, status_holder)
        try:
            try:
                result = await synapse.run(user_input, session=session)
                last_status = result.status.value
            except KeyboardInterrupt:
                # Safety net when the active planner has no request_cancel.
                with _swallow("repl: session save on interrupt"):
                    session.save()
                console.print("[yellow]⚠ 已中断，当前进度已保存。[/yellow]")
                exiting[0] = True
                break
        except asyncio.CancelledError:
            # Ctrl+C during task — let the outer KeyboardInterrupt handler deal
            # with it, just clean up spinner and exit the loop.
            exiting[0] = True
            break
        except Exception as exc:
            if use_rich:
                console.print(f"  [bold red]{_friendly_error(exc)}[/bold red]")
            else:
                print(_friendly_error(exc))
            continue
        finally:
            _restore_cancel_handler(prev_handler)
            if live_run is not None:
                live_run.stop()

        _print_result(console, result, use_rich)
        session.save()


# ---- Eval command handler -------------------------------------------------


def _eval_report_path(args) -> Path:
    if args.report:
        return Path(args.report).expanduser()
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    return Path("eval-results") / f"{args.benchmark}-{stamp}.json"


def _workspace_identity(workspace: str | Path) -> dict:
    """Return a path-free fingerprint for an explicit evaluation workspace."""
    import subprocess

    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"evaluation workspace is not a directory: {workspace}")
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=True, timeout=10,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z"], cwd=root,
            capture_output=True, check=True, timeout=30,
        ).stdout
        diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD"], cwd=root,
            capture_output=True, check=True, timeout=60,
        ).stdout
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=root,
            capture_output=True, check=True, timeout=30,
        ).stdout.split(b"\0")
        digest = hashlib.sha256(commit.encode("utf-8") + b"\0" + status + diff)
        for raw_relative in sorted(item for item in untracked if item):
            relative = raw_relative.decode("utf-8", errors="surrogateescape")
            target = (root / relative).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                continue
            digest.update(raw_relative + b"\0")
            with target.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        return {
            "kind": "git",
            "commit": commit,
            "dirty": bool(status),
            "state_sha256": digest.hexdigest(),
        }
    except (OSError, subprocess.SubprocessError):
        digest = hashlib.sha256()
        file_count = 0
        excluded = {".git", ".synapse", ".venv", "venv", "node_modules", "__pycache__"}
        # ponytail: generated dependency/state trees are excluded; formal runs
        # should identify those through the container image rather than hash them.
        for target in sorted(root.rglob("*")):
            if not target.is_file() or any(
                part in excluded for part in target.relative_to(root).parts
            ):
                continue
            relative = target.relative_to(root).as_posix()
            digest.update(relative.encode("utf-8") + b"\0")
            try:
                with target.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError:
                continue
            file_count += 1
        return {
            "kind": "directory",
            "file_count": file_count,
            "state_sha256": digest.hexdigest(),
        }


def _build_eval_config(args, provider: str, model: str, isolation: str) -> dict:
    """Return the effective, fingerprintable config for one evaluation run."""
    config, _ = load_config()
    payload = config.model_dump(mode="json")
    payload.setdefault("provider", {})["provider"] = provider
    payload["provider"]["model"] = model
    payload.setdefault("tools", {})["workspace_root"] = "<evaluation-workspace>"
    for section_name, key in (("security", "allowed_paths"), ("plugins", "paths")):
        values = payload.get(section_name, {}).get(key, [])
        if isinstance(values, list):
            payload[section_name][key] = [
                f"<absolute>/{Path(value).name}"
                if Path(str(value)).expanduser().is_absolute() else str(value)
                for value in values
            ]
    evaluation = {
        "benchmark": args.benchmark,
        "repeat": args.repeat,
        "max_tasks": getattr(args, "max_tasks", None),
        "isolation": isolation,
        "auto_approve": True,
    }
    dataset = getattr(args, "dataset", None)
    if dataset:
        source = Path(dataset).expanduser().resolve()
        evaluation["dataset"] = {
            "name": source.name,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }
    for key in ("dataset_version", "dataset_source", "dataset_license"):
        value = getattr(args, key, None)
        if value:
            evaluation[key] = value
    workspace = getattr(args, "workspace", None)
    if workspace:
        evaluation["workspace"] = _workspace_identity(workspace)
    payload["evaluation"] = evaluation
    return payload


def _print_eval_result(result, report_path: Path) -> None:
    dashboard_path = result.write_html(report_path.with_suffix(".html"))
    csv_path = result.write_csv(report_path.with_suffix(".csv"))
    print("\n--- Results ---")
    task_rate = (
        f"{result.task_success_rate:.1%}"
        if result.scored_task_total else "n/a"
    )
    attempt_rate = (
        f"{result.attempt_pass_rate:.1%}"
        if result.scored_attempt_total else "n/a"
    )
    print(f"Tasks:      {result.task_succeeded}/{result.scored_task_total} "
          f"success@{result.task_success_k} ({task_rate}); "
          f"{result.task_total} scheduled")
    print(f"Attempts:   {result.attempt_passed}/{result.scored_attempt_total} "
          f"({attempt_rate}); {result.attempt_total} scheduled, "
          f"{result.excluded_attempts} excluded")
    print(f"Completed:  {result.completed}")
    print(f"Failed:     {result.failed}")
    attempt_ci = (
        f"[{result.attempt_pass_rate_ci95[0]:.1%}, "
        f"{result.attempt_pass_rate_ci95[1]:.1%}]"
        if result.scored_attempt_total else "n/a"
    )
    task_ci = (
        f"[{result.task_success_rate_ci95[0]:.1%}, "
        f"{result.task_success_rate_ci95[1]:.1%}]"
        if result.scored_task_total else "n/a"
    )
    print(f"Attempt CI: {attempt_ci}")
    print(f"Task CI:    {task_ci}")
    print("Pass@k:     " + ", ".join(
        f"{k}={value:.1%}" for k, value in result.pass_at_k_by_k.items()
    ))
    print("Pass^k:     " + ", ".join(
        f"{k}={value:.1%}" for k, value in result.pass_power_k_by_k.items()
    ))
    false_success_rate = (
        f"{result.false_success_rate:.1%}"
        if result.false_success_rate is not None else "n/a"
    )
    print(f"False success: {result.false_successes}/{result.verified_agent_reported_successes} "
          f"verified successes ({false_success_rate})")
    print(f"Verification: {result.unverified_attempts} unverified, "
          f"{result.grader_error_attempts} grader errors")
    print(f"Mean score: {result.mean_score:.3f}")
    print(f"Tokens:     {result.tokens_input + result.tokens_output}")
    token_per_pass = (
        f"{result.tokens_per_passed_attempt:.2f}"
        if result.tokens_per_passed_attempt is not None else "n/a"
    )
    cost_per_pass = (
        f"${result.cost_per_passed_attempt_usd:.6f}"
        if result.cost_per_passed_attempt_usd is not None else "n/a"
    )
    token_per_task = (
        f"{result.tokens_per_succeeded_task:.2f}"
        if result.tokens_per_succeeded_task is not None else "n/a"
    )
    cost_per_task = (
        f"${result.cost_per_succeeded_task_usd:.6f}"
        if result.cost_per_succeeded_task_usd is not None else "n/a"
    )
    print(f"Tokens/pass:{token_per_pass}")
    print(f"Est. cost:  ${result.total_cost_usd:.6f} "
          f"({cost_per_pass}/pass)")
    print(f"Per task:   {token_per_task} tokens, {cost_per_task} estimated cost")
    print(f"Tool rate:  {result.tool_success_rate:.1%}")
    print(f"Latency:    median={result.median_duration_ms:.0f}ms "
          f"p95={result.p95_duration_ms:.0f}ms")
    print(f"Report:     {report_path.resolve()}")
    print(f"Dashboard:  {dashboard_path}")
    print(f"CSV:        {csv_path}")
    for task in result.results:
        mark = "+" if task.passed else "!"
        print(
            f"  [{mark}] {task.task_id}: {task.status} "
            f"score={task.score:.2f} ({task.duration_ms}ms)"
        )


async def _run_repo_pytest_eval(args, provider: str, model: str, report_path: Path) -> None:
    """Run the isolated local functional fixture and persist a normal report."""
    from synapse.adapters.library import Synapse
    from synapse.eval.benchmarks.repo_pytest import RepoPytestBenchmark
    from synapse.eval.runner import BenchmarkRunner
    from synapse.protocols.planner import AgentResult, ResultStatus

    async def approve(_request) -> bool:
        return True

    async def run_agent(task: str, root: Path):
        agent = Synapse(
            provider=provider,
            model=model,
            enable_eval=True,
            workspace_root=str(root),
            confirm_callback=approve,
        )
        try:
            result = await agent.run(task, confirm_callback=approve)
            return result, agent.get_run_score()
        finally:
            await agent.aclose()

    fixture = RepoPytestBenchmark()
    benchmark = fixture.benchmark()

    async def run_fixture(_task):
        outcome = await fixture.run(
            run_agent,
            trusted_host_execution=getattr(args, "trusted_host_execution", False),
        )
        agent_result = outcome.agent_result
        if agent_result is None:
            agent_result = AgentResult(ResultStatus.FAILED, "agent did not return a result")
        return agent_result, {"repo_pytest": outcome.to_dict(), "runtime": outcome.run_score}

    async def unused_run_task(_description):
        raise RuntimeError("repo_pytest requires the task-aware runner")

    repeat = int(getattr(args, "repeat", 1))
    result = await BenchmarkRunner().run(
        benchmark,
        unused_run_task,
        task_runner=run_fixture,
        repeat=repeat,
        report_path=report_path,
        metadata={
            "provider": provider,
            "model": model,
            "isolation": "temporary_git_repo",
        },
        evaluation_config=_build_eval_config(
            args, provider, model, "temporary_git_repo",
        ),
    )
    _print_eval_result(result, report_path)


async def _run_swebench_eval(args, provider: str, model: str, report_path: Path) -> None:
    """Run SWE-bench tasks with a fresh checkout and private-test grader."""
    import shlex
    import subprocess
    import tempfile

    from synapse.adapters.library import Synapse
    from synapse.eval.benchmarks.swebench import SWEBenchAdapter
    from synapse.eval.runner import BenchmarkRunner

    if not args.dataset:
        print("swebench requires a local JSONL dataset; pass --dataset PATH")
        return
    benchmark = SWEBenchAdapter.benchmark(args.dataset, args.max_tasks)
    if not benchmark.tasks:
        print("swebench requires a local JSONL dataset; pass --dataset PATH")
        return
    trusted_host_execution = getattr(args, "trusted_host_execution", False)
    SWEBenchAdapter.require_trusted_host_execution(trusted_host_execution)

    async def approve(_request) -> bool:
        return True

    async def run_task(task):
        metadata = task.metadata
        repo_url = str(metadata.get("repo_url") or metadata.get("repo") or "").strip()
        if repo_url and "://" not in repo_url and not Path(repo_url).exists():
            repo_url = f"https://github.com/{repo_url}.git"
        if not repo_url:
            raise ValueError(f"SWE-bench task {task.id} is missing repo/repo_url")
        base_commit = str(metadata.get("base_commit") or metadata.get("environment_setup_commit") or "")
        if not base_commit:
            raise ValueError(f"SWE-bench task {task.id} is missing base_commit")
        with tempfile.TemporaryDirectory(prefix="synapse-swebench-agent-") as tmp:
            root = Path(tmp) / "repo"
            subprocess.run(
                ["git", "clone", "--quiet", repo_url, str(root)],
                capture_output=True, text=True, check=True, timeout=900,
            )
            subprocess.run(
                ["git", "checkout", "--quiet", base_commit],
                cwd=root, capture_output=True, text=True, check=True, timeout=120,
            )
            synapse = Synapse(
                enable_eval=True,
                provider=provider,
                model=model,
                workspace_root=str(root),
                confirm_callback=approve,
            )
            try:
                agent_result = await synapse.run(task.description, confirm_callback=approve)
                patch = SWEBenchAdapter.extract_patch(root)
                private_tests = metadata.get("private_tests") or {}
                if not isinstance(private_tests, dict):
                    private_tests = {}
                test_command = metadata.get("test_command")
                if isinstance(test_command, str):
                    test_command = shlex.split(test_command)
                execution = SWEBenchAdapter.execute(
                    str(root), base_commit, patch, private_tests,
                    test_command=test_command,
                    timeout=int(metadata.get("timeout", 900)),
                    private_test_patch=str(metadata.get("test_patch") or ""),
                    trusted_host_execution=trusted_host_execution,
                )
                facts = {
                    "applied": execution.applied,
                    "tests_passed": execution.passed,
                    "private_tests_applied": execution.private_tests_applied,
                    "changed_files": execution.changed_files,
                    "output": execution.output,
                    "patch_chars": len(patch),
                    "llm_call_count": getattr(agent_result.metrics, "llm_call_count", 0),
                    "llm_time_ms": getattr(agent_result.metrics, "llm_time_ms", 0),
                    "runtime": synapse.get_run_score(),
                }
                return agent_result, {"swebench": facts}
            finally:
                await synapse.aclose()

    print(f"Tasks:     {len(benchmark.tasks)}")
    result = await BenchmarkRunner().run(
        benchmark,
        lambda _description: run_task(benchmark.tasks[0]),
        task_runner=run_task,
        repeat=args.repeat,
        report_path=report_path,
        metadata={
            "provider": provider,
            "model": model,
            "isolation": "temporary_git_checkout",
            "trusted_host_execution": trusted_host_execution,
        },
        evaluation_config=_build_eval_config(
            args, provider, model, "temporary_git_checkout",
        ),
    )
    _print_eval_result(result, report_path)


async def _run_terminal_eval(args, provider: str, model: str, report_path: Path) -> None:
    """Run Terminal-Bench-compatible tasks in isolated temporary workspaces."""
    import tempfile

    from synapse.adapters.library import Synapse
    from synapse.eval.benchmarks.terminal import TerminalBenchAdapter, TerminalSmokeBenchmark
    from synapse.eval.runner import BenchmarkRunner

    if args.benchmark == "terminal_smoke":
        benchmark = TerminalSmokeBenchmark.benchmark()
    else:
        tasks = TerminalBenchAdapter.tasks(args.dataset, args.max_tasks)
        if not tasks:
            print("terminal_bench requires a local JSON/JSONL dataset; pass --dataset PATH")
            return
        benchmark = TerminalBenchAdapter.benchmark(tasks)
    trusted_host_execution = getattr(args, "trusted_host_execution", False)
    TerminalBenchAdapter.require_trusted_host_execution(
        benchmark.tasks, trusted_host_execution,
    )

    def prepare_workspace(task, root: Path) -> None:
        for relative, content in (task.metadata.get("setup_files") or {}).items():
            target = (root / str(relative)).resolve()
            if not target.is_relative_to(root.resolve()):
                raise ValueError(f"setup path escapes workspace: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content), encoding="utf-8")

    # Validate the entire dataset before spending model budget.  A bad task
    # must fail the run, not disappear into the excluded-pair denominator.
    for task in benchmark.tasks:
        with tempfile.TemporaryDirectory(prefix="synapse-terminal-preflight-") as tmp:
            root = Path(tmp)
            prepare_workspace(task, root)
            TerminalBenchAdapter.preflight(
                task,
                root,
                trusted_host_execution=trusted_host_execution,
            )

    async def approve(_request) -> bool:
        return True

    async def run_task(task):
        with tempfile.TemporaryDirectory(prefix="synapse-terminal-eval-") as tmp:
            root = Path(tmp)
            prepare_workspace(task, root)
            TerminalBenchAdapter.preflight(
                task,
                root,
                trusted_host_execution=trusted_host_execution,
            )
            synapse = Synapse(
                enable_eval=True,
                provider=provider,
                model=model,
                workspace_root=str(root),
                confirm_callback=approve,
            )
            from synapse.core.events import EventBus
            event_bus = synapse._container.resolve(EventBus)
            trajectory: list[dict] = []
            event_types = (
                "agent_progress", "llm_token", "tool_call_started",
                "tool_call_completed", "agent_completed",
            )
            handlers = {}
            for event_type in event_types:
                async def _record(event, event_type=event_type):
                    trajectory.append({
                        "type": event_type,
                        "payload": dict(getattr(event, "__dict__", {})),
                    })
                handlers[event_type] = _record
                event_bus.subscribe(event_type, _record)
            try:
                agent_result = await synapse.run(task.description, confirm_callback=approve)
            finally:
                for event_type, handler in handlers.items():
                    event_bus.unsubscribe(event_type, handler)
            try:
                facts = TerminalBenchAdapter.grade_workspace(
                    task,
                    root,
                    trusted_host_execution=trusted_host_execution,
                )
                facts["runtime"] = synapse.get_run_score()
                facts["trajectory"] = trajectory[-200:]
                return agent_result, {"terminal": facts}
            finally:
                await synapse.aclose()

    print(f"Tasks:     {len(benchmark.tasks)}")
    result = await BenchmarkRunner().run(
        benchmark,
        lambda _description: run_task(benchmark.tasks[0]),
        task_runner=run_task,
        repeat=args.repeat,
        report_path=report_path,
        metadata={
            "provider": provider,
            "model": model,
            "isolation": "temporary_workspace",
            "official_runner": benchmark.metadata.get("official_runner"),
        },
        evaluation_config=_build_eval_config(
            args, provider, model, "temporary_workspace",
        ),
    )
    _print_eval_result(result, report_path)


async def _run_eval(args) -> None:
    """Execute a named benchmark via the Synapse facade."""
    from synapse.adapters.library import Synapse
    from synapse.eval.runner import Benchmark, BenchmarkRunner

    config, _ = load_config()
    provider = args.provider or config.provider.provider
    model = args.model or config.provider.model
    print(f"Benchmark: {args.benchmark}")
    print(f"Provider:  {provider}/{model}")
    report_path = _eval_report_path(args)

    if args.benchmark == "repo_pytest":
        try:
            await _run_repo_pytest_eval(args, provider, model, report_path)
        except Exception as exc:
            print(f"Evaluation unavailable: {exc}")
        return

    if args.benchmark == "swebench":
        try:
            await _run_swebench_eval(args, provider, model, report_path)
        except Exception as exc:
            print(f"Evaluation unavailable: {exc}")
        return

    if args.benchmark in {"terminal_smoke", "terminal_bench"}:
        try:
            await _run_terminal_eval(args, provider, model, report_path)
        except Exception as exc:
            print(f"Evaluation unavailable: {exc}")
        return

    if args.benchmark == "process_quality":
        from synapse.eval.benchmarks.process_bench import ProcessQualityBenchmark
        tasks = ProcessQualityBenchmark.tasks()
        if args.max_tasks is not None:
            tasks = tasks[:max(0, args.max_tasks)]
        benchmark = ProcessQualityBenchmark.benchmark(tasks)
    else:
        print(f"Unknown benchmark: {args.benchmark}")
        return

    print(f"Tasks:     {len(tasks)}")

    async def approve(_request) -> bool:
        return True

    import tempfile

    async def run_task(task):
        if args.workspace:
            workspace_root = Path(args.workspace).expanduser().resolve()
            agent = Synapse(
                enable_eval=True,
                provider=provider,
                model=model,
                workspace_root=str(workspace_root),
                confirm_callback=approve,
            )
            try:
                result = await agent.run(task.description, confirm_callback=approve)
                return result, agent.get_run_score()
            finally:
                await agent.aclose()
        with tempfile.TemporaryDirectory(prefix=f"synapse-eval-{args.benchmark}-") as tmp:
            agent = Synapse(
                enable_eval=True,
                provider=provider,
                model=model,
                workspace_root=tmp,
                confirm_callback=approve,
            )
            try:
                result = await agent.run(task.description, confirm_callback=approve)
                return result, agent.get_run_score()
            finally:
                await agent.aclose()

    async def unused_run_task(_description):
        raise RuntimeError("process_quality requires the task-aware runner")

    isolation = "explicit_shared_workspace" if args.workspace else "temporary_workspace_per_attempt"
    result = await BenchmarkRunner().run(
        benchmark,
        unused_run_task,
        task_runner=run_task,
        repeat=args.repeat,
        report_path=report_path,
        metadata={
            "provider": provider,
            "model": model,
            "workspace_mode": "explicit" if args.workspace else "temporary_per_attempt",
            "state_isolated": not bool(args.workspace),
        },
        evaluation_config=_build_eval_config(args, provider, model, isolation),
    )
    _print_eval_result(result, report_path)


# ---- Experiment command handler -------------------------------------------


async def _run_experiment(args) -> None:
    """Execute an A/B experiment."""
    import json as _json
    import tempfile
    from contextlib import ExitStack

    from synapse.eval.experiments import Experiment
    from synapse.eval.runner import (
        _fingerprint,
        _runner_comparability_envelope,
        _sanitize_config,
    )

    config_a = _json.loads(args.config_a)
    config_b = _json.loads(args.config_b)

    from synapse.adapters.library import Synapse

    dataset_path = None
    task_benchmark = None
    trusted_host_execution = bool(getattr(args, "trusted_host_execution", False))
    if getattr(args, "dataset", None):
        from synapse.eval.benchmarks.terminal import TerminalBenchAdapter

        dataset_path = Path(args.dataset).expanduser().resolve()
        tasks = TerminalBenchAdapter.tasks(
            dataset_path, getattr(args, "max_tasks", None),
        )
        if not tasks:
            raise ValueError("experiment dataset contains no runnable tasks")
        TerminalBenchAdapter.require_trusted_host_execution(
            tasks, trusted_host_execution,
        )
        task_benchmark = TerminalBenchAdapter.benchmark(tasks)
        task_benchmark.metadata["dataset"] = {
            "name": dataset_path.name,
            "sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        }
        task_benchmark.metadata["dataset_manifest"].update({
            "name": dataset_path.stem,
            "selection_max_tasks": getattr(args, "max_tasks", None),
        })

    primary_metric = (
        getattr(args, "primary_metric", None)
        or ("functional_success" if task_benchmark is not None else "duration_ms")
    )
    print(f"Experiment: {args.name}")
    print(f"Config A:   {_json.dumps(_sanitize_config(config_a))}")
    print(f"Config B:   {_json.dumps(_sanitize_config(config_b))}")
    if task_benchmark is None:
        print(f"Task:       {args.task}")
        print("Success:    agent_reported_success is Harness status, not an external grader")
    else:
        print(f"Dataset:    {dataset_path.name}")
        print(f"Tasks:      {len(task_benchmark.tasks)}")
        print("Success:    functional_success comes from the external workspace grader")
    print(f"Runs:       {args.runs}")
    print("Seed:       controls pair ordering/statistics; model sampling is provider-defined")
    print()

    async def resolve_effective_config(config: dict) -> dict:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="synapse-experiment-config-") as tmp:
            run_config = dict(config)
            run_config["enable_eval"] = True
            run_config["workspace_root"] = tmp
            run_config["strict_overrides"] = True
            synapse = Synapse(**run_config)
            try:
                return synapse.get_effective_config()
            finally:
                close = getattr(synapse, "aclose", None)
                if close is not None:
                    await close()

    effective_config_a = await resolve_effective_config(config_a)
    effective_config_b = await resolve_effective_config(config_b)

    async def diagnostic_benchmark(config: dict, _seed: int):
        run_config = dict(config)
        run_config["enable_eval"] = True
        run_config["strict_overrides"] = True
        synapse = Synapse(**run_config)
        try:
            result = await synapse.run(args.task)
            score = synapse.get_run_score() or {}
            safety = score.get("safety", {})
            metrics = result.metrics
            effective = synapse.get_effective_config()
            return ({
                "agent_reported_success": float(result.status.value == "success"),
                "duration_ms": float(metrics.duration_ms),
                "tokens": float(metrics.tokens_input + metrics.tokens_output),
                "tool_calls": float(metrics.tool_call_count),
                "tool_success_rate": (
                    metrics.tool_success_count / metrics.tool_call_count
                    if metrics.tool_call_count else 0.0
                ),
                "safety_risk_attempts": float(
                    safety.get("injection_attempts", 0)
                    + safety.get("dangerous_command_attempts", 0)
                ),
                "safety_policy_blocks": float(safety.get("auth_blocks", 0)),
                "safety_violations": float(
                    safety.get("sandbox_violations", 0)
                    + safety.get("out_of_workspace_access", 0)
                ),
            }, _runner_comparability_envelope(effective, score))
        finally:
            close = getattr(synapse, "aclose", None)
            if close is not None:
                await close()

    workspace_stack = ExitStack()
    setup_by_group: dict[str, dict[str, object]] = {}
    if task_benchmark is not None:
        for task in task_benchmark.tasks:
            group = str(task.metadata.get("sequence_id") or task.id)
            setup = setup_by_group.setdefault(group, {})
            for relative, content in (task.metadata.get("setup_files") or {}).items():
                key = str(relative)
                if key in setup and setup[key] != content:
                    raise ValueError(
                        f"sequence setup file has conflicting contents: {key}"
                    )
                setup[key] = content

    baseline_ids = {
        group: _fingerprint({
            "kind": "terminal_dataset_workspace",
            "group": group,
            "setup_files": setup,
        })
        for group, setup in setup_by_group.items()
    }
    baseline_ids.setdefault(
        "callback", _fingerprint({"kind": "empty_workspace", "task": args.task}),
    )

    if task_benchmark is not None:
        from synapse.eval.benchmarks.terminal import TerminalBenchAdapter

        for task in task_benchmark.tasks:
            group = str(task.metadata.get("sequence_id") or task.id)
            with tempfile.TemporaryDirectory(
                prefix="synapse-experiment-preflight-",
            ) as tmp:
                root = Path(tmp).resolve()
                for relative, content in setup_by_group.get(group, {}).items():
                    target = (root / relative).resolve()
                    if not target.is_relative_to(root):
                        raise ValueError(f"setup path escapes workspace: {relative}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(str(content), encoding="utf-8")
                TerminalBenchAdapter.preflight(
                    task,
                    root,
                    trusted_host_execution=trusted_host_execution,
                )
        task_benchmark.metadata["baseline_preflight"] = "passed"

    def workspace_factory(*, label: str, task_id: str, attempt: int) -> dict:
        path = workspace_stack.enter_context(
            tempfile.TemporaryDirectory(
                prefix=f"synapse-experiment-{label.lower()}-{attempt}-",
            ),
        )
        root = Path(path).resolve()
        for relative, content in setup_by_group.get(task_id, {}).items():
            target = (root / relative).resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"setup path escapes workspace: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content), encoding="utf-8")
        return {
            "path": path,
            "baseline_id": baseline_ids.get(task_id, baseline_ids["callback"]),
        }

    async def run_dataset_task(config: dict, task, _seed: int, attempt: int = 1):
        from synapse.eval.benchmarks.terminal import TerminalBenchAdapter

        async def approve(_request) -> bool:
            return True

        run_config = dict(config)
        run_config["enable_eval"] = True
        run_config["strict_overrides"] = True
        run_config["confirm_callback"] = approve
        synapse = Synapse(**run_config)
        try:
            TerminalBenchAdapter.preflight(
                task,
                run_config["workspace_root"],
                trusted_host_execution=trusted_host_execution,
            )
            agent_result = await synapse.run(task.description, confirm_callback=approve)
            score = synapse.get_run_score() or {}
            score.setdefault("efficiency", {})
            score.setdefault("safety", {})
            facts = TerminalBenchAdapter.grade_workspace(
                task,
                run_config["workspace_root"],
                trusted_host_execution=trusted_host_execution,
            )
            run_score = {"terminal": facts, "runtime": score, "attempt": attempt}
            run_score.update(
                _runner_comparability_envelope(synapse.get_effective_config(), score)
            )
            return agent_result, run_score
        finally:
            close = getattr(synapse, "aclose", None)
            if close is not None:
                await close()

    import uuid
    metric_directions = {
        "functional_success": "higher",
        "grader_score": "higher",
        "agent_reported_success": "higher",
        "duration_ms": "lower",
        "tokens": "lower",
        "tool_calls": "lower",
        "tool_success_rate": "higher",
        "safety_risk_attempts": "lower",
        "safety_policy_blocks": "lower",
        "safety_violations": "lower",
    }
    experiment = Experiment(
        id=str(uuid.uuid4()),
        name=args.name,
        variables=(
            {
                "dataset": {
                    "name": dataset_path.name,
                    "sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
                },
                "task_count": len(task_benchmark.tasks),
            }
            if task_benchmark is not None else {"task": args.task}
        ),
        agent_config_a=config_a,
        agent_config_b=config_b,
        benchmark=task_benchmark or diagnostic_benchmark,
        effective_config_a=effective_config_a,
        effective_config_b=effective_config_b,
        runs_per_config=args.runs,
        primary_metric=primary_metric,
        direction=args.direction or metric_directions[primary_metric],
        seed=args.seed,
        metric_directions=(
            {
                name: direction for name, direction in metric_directions.items()
                if name not in {"functional_success", "grader_score"}
            }
            if task_benchmark is None else {}
        ),
        guardrail_metrics=tuple(
            metric for metric in (
                (("functional_success", "safety_violations")
                 if task_benchmark is not None else ("safety_violations",))
            ) if metric != primary_metric
        ),
        allowed_config_diff_paths=(
            tuple(args.allowed_config_diff) if args.allowed_config_diff else None
        ),
        workspace_factory=workspace_factory,
        task_runner=run_dataset_task if task_benchmark is not None else None,
    )

    print("Running experiment...")
    try:
        result = await experiment.run()
    finally:
        workspace_stack.close()

    print(f"\n--- Results ---")
    print(f"Primary metric:   {result.primary_metric} ({result.direction} is better)")
    for name, comparison in result.metric_results.items():
        ci = comparison.bootstrap_ci
        ci_text = f"[{ci[0]:.4g}, {ci[1]:.4g}]" if ci else "n/a"
        print(
            f"{name}: A={comparison.mean_a:.4g} B={comparison.mean_b:.4g} "
            f"delta={comparison.mean_delta:.4g} CI={ci_text} p={comparison.p_value}"
        )
    if result.guardrail_regressions:
        print(f"Guardrail regressions: {', '.join(result.guardrail_regressions)}")
    if result.comparability_issues:
        print(f"Comparability:    {', '.join(result.comparability_issues)}")
    print(f"Outcome:          {result.outcome}")
    safe_name = "".join(
        char if char.isalnum() or char in "-_" else "-" for char in args.name
    ).strip("-") or "experiment"
    report_path = Path(args.report).expanduser() if args.report else (
        Path("eval-results")
        / f"experiment-{safe_name}-{_time.strftime('%Y%m%d-%H%M%S')}.json"
    )
    print(f"Report:           {result.write_json(report_path)}")
    print(f"HTML:             {result.write_html(report_path.with_suffix('.html'))}")


if __name__ == "__main__":
    main()
