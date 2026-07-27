"""CLI entry point for Synapse."""

import argparse
import asyncio
import os as _os
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
from synapse.core.agent import Agent
from synapse.core.session import Session
from synapse.core.events import EventBus
from synapse.core.exceptions import (
    SynapseError,
    ConfigError,
    ProviderError,
    ToolError,
    SandboxError,
    PlannerError,
)




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
    # Serialize prompts so concurrent swarm workers can't interleave reads on
    # the shared stdin (they all share this one callback instance).
    _prompt_lock = asyncio.Lock()

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

        # Permanent allow list (session-scoped: "yes to all" for this tool).
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


class _LiveDisplay:
    """Rich Live panel that shows streamed LLM text with a live footer.

    The footer reports the current activity label plus optional token/elapsed
    readouts supplied by the caller. Used to render the agent's streaming
    output in real time (replaces the old spinner-only status).

    Refresh cadence is owned by a dedicated background thread (see
    ``_refresh_loop``), NOT by rich's ``auto_refresh`` and NOT by an asyncio
    task. Rationale: the elapsed clock only advances when the panel is
    actually redrawn, and the redraw must happen on a steady cadence
    *independent* of the asyncio event loop. The old design relied on rich's
    auto_refresh thread plus a per-0.5s asyncio ``_tick`` that merely set the
    renderable (``live.update(refresh=False)`` performs no screen write), so
    during a fast token stream the main thread hogged the GIL doing
    ``"".join(self._buf)`` on a growing buffer and starved the redraw thread —
    the clock froze, then snapped forward when the burst ended. Owning the
    thread and bounding the buffer keeps redraws cheap and steady.
    """

    # Keep the rendered transcript bounded so each redraw is O(1) regardless of
    # how long a single generation runs. Older text scrolls out of the panel.
    _MAX_BUF_CHARS = 4000

    def __init__(self, console, fmt_tokens, fmt_elapsed):
        from rich.live import Live
        from rich.panel import Panel
        from rich.text import Text

        self._console = console
        self._fmt_tokens = fmt_tokens
        self._fmt_elapsed = fmt_elapsed
        self._buf: list[str] = []
        self._buf_len = 0
        self._label = "Thinking..."
        self._swarm_lines: list[str] = []
        # auto_refresh=False: we drive screen writes from our own thread so the
        # cadence never depends on the event loop or on event-handler storms.
        self._live = Live(self._render(), console=console, auto_refresh=False)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._refresh_loop, daemon=True)

    @property
    def live(self):
        return self._live

    def start(self):
        self._live.start()
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            self._thread.join(timeout=0.5)
        except Exception:
            pass
        try:
            self._live.stop()
        except Exception:
            pass

    def _refresh_loop(self):
        """Force a screen write every ~0.2s on a thread independent of the
        event loop, so the clock/token readouts stay live even while the agent
        loop is busy streaming or executing tools."""
        while not self._stop.is_set():
            self._stop.wait(0.2)
            try:
                self._live.refresh()
            except Exception:
                pass

    def set_label(self, text: str) -> None:
        self._label = text
        self._refresh()

    def add_text(self, text: str) -> None:
        self._buf.append(text)
        self._buf_len += len(text)
        # Drop oldest chunks once we exceed the cap so joins stay bounded.
        while self._buf_len > self._MAX_BUF_CHARS and len(self._buf) > 1:
            dropped = self._buf.pop(0)
            self._buf_len -= len(dropped)
        self._refresh()

    def reset_text(self) -> None:
        self._buf = []
        self._buf_len = 0
        self._refresh()

    def set_swarm_lines(self, lines: list[str]) -> None:
        """Replace the swarm-status footer lines shown under the streamed text."""
        self._swarm_lines = list(lines)
        self._refresh()

    def _refresh(self) -> None:
        try:
            self._live.update(self._render())
        except Exception:
            pass

    def _render(self):
        from rich.panel import Panel
        from rich.text import Text

        body = "".join(self._buf)
        style = _status_style_for(self._label)
        pieces = [f"[{style}]● {self._label}[/{style}]"]
        tk = self._fmt_tokens()
        if tk:
            pieces.append(f"[dim]{tk} tok[/dim]")
        el = self._fmt_elapsed()
        if el:
            pieces.append(f"[dim]{el}[/dim]")
        header = "  ·  ".join(pieces)
        text = Text(body, style="none") if body else Text("…", style="dim")
        if self._swarm_lines:
            text.append("\n\n")
            for line in self._swarm_lines:
                text.append(line + "\n", style="cyan")
        return Panel(text, title=header, border_style=_BORDER, expand=True)


class _SwarmTracker:
    """Collects swarm lifecycle events into a compact summary for the live panel.

    Wires the five swarm event types on an EventBus, keeps just enough state to
    render "how many workers / who was rejected / retried / verified", and calls
    ``on_update`` with the rendered lines so the caller can push them to a
    :class:`_LiveDisplay`.  Reused by the ``run`` subcommand and the REPL.
    """

    _EVENTS = ("worker_spawned", "worker_completed",
               "review_submitted", "vote_cast", "swarm_verified")

    def __init__(self, on_update):
        self._on_update = on_update
        self.workers: dict[str, dict] = {}
        self.reviews: list = []
        self.votes: list = []
        self.verified: str | None = None
        self._handlers: dict[str, object] = {}

    def render_lines(self) -> list[str]:
        if not self.workers and self.verified is None:
            return []
        lines = []
        for wid, w in self.workers.items():
            lines.append(f"[swarm] {w['role']} ({wid}): {w['status']}")
        if self.reviews or self.votes:
            rejects = sum(1 for r in self.reviews if getattr(r, "verdict", "") == "reject")
            lines.append(f"[swarm] reviews={len(self.reviews)} rejected={rejects} votes={len(self.votes)}")
        if self.verified is not None:
            lines.append(f"[swarm] verified: {self.verified}")
        return lines

    def wire(self, event_bus) -> None:
        async def _on_spawned(ev):
            self.workers[ev.agent_id] = {"role": ev.role, "status": "running"}
            self._on_update(self.render_lines())

        async def _on_completed(ev):
            w = self.workers.get(ev.agent_id)
            if w:
                w["status"] = ev.status
            self._on_update(self.render_lines())

        async def _on_review(ev):
            self.reviews.append(ev)
            self._on_update(self.render_lines())

        async def _on_vote(ev):
            self.votes.append(ev)
            self._on_update(self.render_lines())

        async def _on_verified(ev):
            self.verified = ev.status
            self._on_update(self.render_lines())

        for et, h in (("worker_spawned", _on_spawned),
                      ("worker_completed", _on_completed),
                      ("review_submitted", _on_review),
                      ("vote_cast", _on_vote),
                      ("swarm_verified", _on_verified)):
            event_bus.subscribe(et, h)
            self._handlers[et] = h

    def unwire(self, event_bus) -> None:
        for et, h in self._handlers.items():
            try:
                event_bus.unsubscribe(et, h)
            except Exception:
                pass


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

    tokens = {"input": 0, "output": 0}
    # Session total before the current LLM request. Captured at the start of
    # each request so streamed per-chunk usage (which is cumulative for that
    # request) can be shown as baseline + request_usage, ticking up smoothly
    # instead of jumping once at the end. The authoritative "tokens=" message
    # still overwrites with the real cumulative total as a safety net.
    baseline = {"in": 0, "out": 0}
    elapsed = {"start": _time.monotonic()}

    def _fmt_tokens() -> str:
        t = tokens["input"] + tokens["output"]
        return f"{t / 1000:.1f}k" if t >= 1000 else str(t)

    def _fmt_elapsed() -> str:
        s = _time.monotonic() - elapsed["start"]
        return f"{s:.0f}s" if s < 60 else f"{int(s // 60)}m{int(s % 60):02d}s"

    live = _LiveDisplay(console, _fmt_tokens, _fmt_elapsed)
    live.start()
    if status_holder is not None:
        status_holder[:] = [live.live]

    event_bus = synapse._container.resolve(EventBus)

    async def _on_progress(event):
        msg = event.message
        if event.phase == "calling_llm":
            # Snapshot the session total before this request so streamed usage
            # increments from this baseline. The preceding request's "tokens="
            # message already set the authoritative cumulative here.
            baseline["in"] = tokens["input"]
            baseline["out"] = tokens["output"]
            live.reset_text()
            live.set_label("Working...")
            return
        if msg.startswith("tokens="):
            try:
                a, b = msg[7:].split("+", 1)
                tokens["input"] = int(a)
                tokens["output"] = int(b)
                live.set_label("Working...")
                return
            except (ValueError, IndexError):
                pass
        live.set_label(msg)

    async def _on_token(event):
        live.add_text(event.text)
        if event.usage:
            u_in = event.usage.get("input", 0) or 0
            u_out = event.usage.get("output", 0) or 0
            tokens["input"] = baseline["in"] + u_in
            tokens["output"] = baseline["out"] + u_out

    async def _on_tool_started(event):
        live.set_label(f"{event.tool_name} ...")

    async def _on_tool_completed(event):
        icon = "ok" if event.success else "FAIL"
        live.set_label(f"{event.tool_name} [{icon}] ({event.duration_ms}ms)")

    if event_bus is not None:
        event_bus.subscribe("agent_progress", _on_progress)
        event_bus.subscribe("llm_token", _on_token)
        event_bus.subscribe("tool_call_started", _on_tool_started)
        event_bus.subscribe("tool_call_completed", _on_tool_completed)
        swarm_tracker = _SwarmTracker(live.set_swarm_lines)
        swarm_tracker.wire(event_bus)

    try:
        return await synapse.run(task, session=session)
    finally:
        live.stop()
        if status_holder is not None:
            status_holder[:] = []
        if event_bus is not None:
            for et, h in (
                ("agent_progress", _on_progress),
                ("llm_token", _on_token),
                ("tool_call_started", _on_tool_started),
                ("tool_call_completed", _on_tool_completed),
            ):
                try:
                    event_bus.unsubscribe(et, h)
                except Exception:
                    pass
            swarm_tracker.unwire(event_bus)


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
            use_rich = True
        except ImportError:
            console = None
            use_rich = False

        try:
            result = asyncio.run(_run_task_streamed(synapse, task, None, console, use_rich))
        except KeyboardInterrupt:
            return
        except Exception as exc:
            if use_rich:
                console.print(f"[bold red]{_friendly_error(exc)}[/bold red]")
            else:
                print(_friendly_error(exc))
            return

        _print_result(console, result, use_rich)
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
                        console.print(f"[bold red]{_friendly_error(exc)}[/bold red]")
                    else:
                        print(_friendly_error(exc))
                    continue
                finally:
                    if use_rich:
                        status.stop()
                        status_holder[:] = []  # clear holder
                        # Unsubscribe event handlers
                        event_bus.unsubscribe("agent_progress", _on_progress)
                        event_bus.unsubscribe("tool_call_started", _on_tool_started)
                        event_bus.unsubscribe("tool_call_completed", _on_tool_completed)

                _print_result(console, result, use_rich)

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


# ---- Slash-command autocomplete ------------------------------------------

#: (command, description) shown in the completion menu, in display order.
_SLASH_COMMANDS: tuple = (
    ("/help",            "Show this help"),
    ("/memory",          "View working memory"),
    ("/session",         "Show session path"),
    ("/reset",           "Clear session state"),
    ("/model",           "Show or switch model"),
    ("/provider",        "Show or switch provider"),
    ("/mode",            "Switch planning mode"),
    ("/tools",           "List available tools"),
    ("/context-report",  "Context block citation heatmap"),
    ("/exit",            "Exit"),
    ("/quit",            "Exit"),
)
_COMPLETION_LIMIT = 6  # max entries shown in the dropdown


def _make_prompt_session():
    """Build a prompt_toolkit session with slash-command completion, or None."""
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.formatted_text import HTML
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

    return PromptSession(completer=_SlashCompleter())


# ---- Main interface -------------------------------------------------------


#: Synapse ASCII art — solid brain with synaptic stem.
_WELCOME_ART = (
    r"        .-=========-.",
    r"     .-'  #########  '-.",
    r"    /   ###  o o  ###   \\",
    r"   |   ####   ~   ####   |",
    r"   |   #####     #####   |",
    r"    \\  '###########'   /",
    r"     '-.  '##### '   .-'",
    r"        '-.__| |__.-'",
    r"            |   |",
    r"         ---+   +---",
)

_WELCOME_NAME = "Synapse"
_WELCOME_SUBTITLE = "connecting ideas into code"
_WELCOME_STATUS = "* ready"

#: Brand palette — single place to tweak the CLI look.
_BRAND = "bright_cyan"          # primary accent (art, name, prompt)
_LABEL = "bold bright_cyan"     # field labels in the banner (same blue family)
_BORDER = "cyan"                # box border — same blue tone as the art
_HINT = "dim"                   # secondary text / hints


def _status_style_for(label: str) -> str:
    """Pick a Rich style for a live-panel status label."""
    low = (label or "").lower()
    if "fail" in low or "error" in low or "budget" in low or "denied" in low:
        return "red"
    if "ok" in low or "completed" in low or "done" in low:
        return "green"
    return _BRAND  # cyan — in-progress / neutral activity


def _print_result(console, result, use_rich: bool) -> None:
    """Render a finished task result consistently across run/chat/REPL."""
    if not use_rich or console is None:
        print(f"\n[Status: {result.status.value}]")
        print(result.output)
        return
    from rich.markdown import Markdown
    from rich.rule import Rule
    color = {"success": "green", "partial": "yellow", "failed": "red"}.get(
        result.status.value, "dim"
    )
    console.print()
    console.print(Rule(style=_BORDER))
    console.print(f"  [{color}]● {result.status.value.upper()}[/{color}]")
    console.print(Markdown(result.output))
    console.print()


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
    gap = 4
    # Label column: icon (2 cells) + label text; values take the rest.
    label_w = 12
    left_w = (inner - gap) // 2
    right_w = inner - gap - left_w

    def _print_plain(text: str, **kwargs) -> None:
        console.print(text, **kwargs)

    def _b(char: str = "=") -> Text:
        return Text(f"+{char * (width - 2)}+", style=_BORDER)

    def _centered(body: str, style: str = "") -> Text:
        """Center *body* in the box.  Leading/trailing whitespace is stripped
        so that the visible content is centred, not the raw string."""
        stripped = body.strip()
        content = _middle(stripped, inner).center(inner) if stripped else " " * inner
        t = Text("| ")
        t.append(content, style=style)
        t.append(" |")
        return t

    def _row(*segments) -> None:
        """Print one boxed row from styled Text segments."""
        line = Text("| ")
        vis = 0
        for seg in segments:
            line.append(seg)
            vis += len(seg)
        if vis < inner:
            line.append(" " * (inner - vis))
        line.append(" |")
        console.print(line)

    def _field(label: str, icon: str, value: str) -> Text:
        """One 'icon LABEL value' field; label fixed-width, value truncated."""
        t = Text()
        t.append(f"{icon} ", style=_BRAND)
        t.append(f"{label:<{label_w - 2}}", style=_LABEL)
        t.append(_middle(str(value), 60))
        return t

    def _pair(l_label: str, l_icon: str, l_val: str,
              r_label: str, r_icon: str, r_val: str) -> None:
        """Two-column row with aligned label columns."""
        l_field = _field(l_label, l_icon, l_val)
        r_field = _field(r_label, r_icon, r_val)
        # Truncate/justify by visible cell length.
        l_vis = f"{l_icon} {l_label:<{label_w - 2}} {_middle(str(l_val), 60)}"
        r_vis = f"{r_icon} {r_label:<{label_w - 2}} {_middle(str(r_val), 60)}"
        l_field = l_field[:max(0, left_w)]
        r_field = r_field[:max(0, right_w)]
        l_cell = Text.assemble(l_field, " " * max(0, left_w - min(len(l_vis), left_w)))
        r_cell = Text.assemble(r_field, " " * max(0, right_w - min(len(r_vis), right_w)))
        _row(l_cell, Text(" " * gap), r_cell)

    # ── render ────────────────────────────────────────────────────────
    console.print(_b("="))
    for art_line in _WELCOME_ART:
        console.print(_centered(art_line, style=_BRAND))
    # Name · subtitle · status on one line
    tagline_plain = f"{_WELCOME_NAME}  |  {_WELCOME_SUBTITLE}  |  {_WELCOME_STATUS}"
    tagline_body = _middle(tagline_plain, inner).center(inner)
    tagline_rich = (
        tagline_body
        .replace(_WELCOME_NAME, f"[bold {_BRAND}]{_WELCOME_NAME}[/bold {_BRAND}]")
        .replace(_WELCOME_SUBTITLE, f"[dim italic]{_WELCOME_SUBTITLE}[/dim italic]")
        .replace(_WELCOME_STATUS, f"[green]{_WELCOME_STATUS}[/green]")
    )
    console.print(f"| {tagline_rich} |")
    console.print(_b("-"))
    _row(Text(""))

    # Workspace row
    ws = _field("WORKSPACE", ">", cwd)
    _row(ws[:inner])

    _pair("MODEL", "*", model, "VERSION", "#", f"v{__version__}")
    _pair("PROVIDER", "@", provider, "PLANNING", "~", config.planning.mode)
    if config_path:
        cfg = Text()
        cfg.append("% ", style=_BRAND)
        cfg.append(_middle(f"config  {config_path}", inner - 2), style=_HINT)
        _row(cfg)

    _row(Text(""))
    console.print(_centered("type /help for commands", style=_HINT))
    console.print(_b("="))


def _show_help(console):
    """Display available commands — pico style."""
    from rich.table import Table
    console.print()
    console.print(f"  [bold {_BRAND}]Commands[/bold {_BRAND}]  [{_HINT}]type a command, or just describe your task[/{_HINT}]")
    t = Table(show_header=False, box=None, padding=(0, 2), pad_edge=False)
    t.add_column(style=f"bold {_BRAND}", no_wrap=True)
    t.add_column(style=_HINT)
    t.add_row("  /help", "Show this help")
    t.add_row("  /memory", "View working memory")
    t.add_row("  /session", "Show session path")
    t.add_row("  /reset", "Clear session state")
    t.add_row("  /model [name|num]", "Show or switch model (number for quick select)")
    t.add_row("  /provider [name]", "Show or switch provider")
    t.add_row("  /mode [name]", "Planning mode (react / plan_execute / hierarchical / swarm)")
    t.add_row("  /tools", "List available tools")
    t.add_row("  /context-report", "Show context block citation / usage heatmap")
    t.add_row("  /score", "Show runtime score (safety/process/quality/efficiency) + hint")
    t.add_row("  /todos", "Show the current todo list (s05)")
    t.add_row("  /exit, /quit", "Exit")
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
        if key:
            avail.append(entry)
        else:
            unavail.append(entry)

    # Custom providers → synthetic ModelEntry objects
    for cp in config.provider.custom_providers:
        key = cp.api_key or main_key
        for model_name in cp.models:
            entry = ModelEntry(provider=cp.name, model=model_name, api_key=cp.api_key, base_url=cp.base_url)
            if key or cp.api_key:
                avail.append(entry)
            else:
                unavail.append(entry)

    return avail, unavail


def _pick_model(console, entries) -> int | None:
    """Interactive arrow-key selector using Rich Live display. Returns index or None."""
    n = len(entries)
    if n == 0:
        return None
    idx = 0
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


def _first_run_wizard(console, config) -> None:
    """Rich-powered interactive setup for first-time users."""
    from synapse.config.schema import _PROVIDER_ENV_VARS

    console.print()
    console.print(f"  [bold {_BRAND}]Welcome to Synapse![/bold {_BRAND}]")
    console.print(f"  [{_HINT}]No API key found. Let's configure your first model.[/{_HINT}]")
    console.print()

    # Show providers
    providers = sorted(_PROVIDER_ENV_VARS.keys())
    console.print("  [bold]Available providers:[/bold]")
    for i, p in enumerate(providers, 1):
        env = _PROVIDER_ENV_VARS[p]
        hint = f"env: {env}" if env else "no key needed"
        console.print(f"  [bold {_BRAND}]{i}.[/bold {_BRAND}] [{_LABEL}]{p}[/{_LABEL}] [{_HINT}]({hint})[/{_HINT}]")

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
        console = Console(force_terminal=True)  # width auto-detected per render
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
    prompt_session = _make_prompt_session() if use_rich else None

    # Show the welcome banner immediately, before heavy imports.
    if use_rich:
        _show_welcome(console, config, config_source)
        _last_cols = console.width
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
        # Re-render banner if terminal resized (font zoom changes column count).
        if use_rich and console.width != _last_cols:
            _last_cols = console.width
            console.print()
            _show_welcome(console, config, config_source)

        try:
            if prompt_session is not None:
                from prompt_toolkit.formatted_text import HTML
                user_input = await prompt_session.prompt_async(
                    HTML('<ansicyan><b>synapse &gt; </b></ansicyan>')
                )
            elif use_rich:
                user_input = console.input(f"  [bold {_BRAND}]synapse > [/bold {_BRAND}]")
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
                session_dir = Path.cwd() / ".synapse" / "sessions"
                if use_rich:
                    console.print(f"[dim]Session path: {session_dir}[/dim]")
                else:
                    print(f"Session path: {session_dir}")
            elif cmd == "/context-report":
                _show_context_report(console, _synapse, use_rich)
            elif cmd == "/score":
                _show_score(console, _synapse, use_rich)
            elif cmd == "/todos":
                _show_todos(console, use_rich)
            elif cmd == "/model":
                if arg:
                    avail, _ = _available_models(config)
                    if arg.isdigit():
                        idx = int(arg) - 1
                        if 0 <= idx < len(avail):
                            entry = avail[idx]
                            provider, model = entry.provider, entry.model
                            _synapse = None
                            prefix = f"[bright_cyan]>[/bright_cyan] [dim]{provider}/{model}[/dim]" if use_rich else f"{provider}/{model}"
                            if use_rich: console.print(prefix)
                            else: print(prefix)
                        else:
                            if use_rich: console.print(f"[red]Invalid number (1-{len(avail)}).[/red]")
                            else: print(f"Invalid number (1-{len(avail)}).")
                    else:
                        candidates = [e for e in avail if e.model == arg or f"{e.provider}/{e.model}" == arg]
                        if candidates:
                            entry = candidates[0]
                            provider, model = entry.provider, entry.model
                            _synapse = None
                            prefix = f"[bright_cyan]>[/bright_cyan] [dim]{provider}/{model}[/dim]" if use_rich else f"{provider}/{model}"
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
                    idx = _pick_model(console, pick_entries)
                    if idx is not None:
                        entry = avail[idx]
                        provider, model = entry.provider, entry.model
                        _synapse = None
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

        if use_rich:
            from rich.status import Status
            tokens = {"input": 0, "output": 0}
            baseline = {"in": 0, "out": 0}
            elapsed = {"start": _time.monotonic(), "label": "Thinking..."}

            def _fmt_tokens() -> str:
                t = tokens["input"] + tokens["output"]
                if t >= 1000:
                    return f"{t/1000:.1f}k"
                return str(t)

            def _fmt_elapsed() -> str:
                s = _time.monotonic() - elapsed["start"]
                if s < 60:
                    return f"{s:.0f}s"
                return f"{int(s//60)}m{int(s%60):02d}s"

            live = _LiveDisplay(console, _fmt_tokens, _fmt_elapsed)
            live.start()
            status_holder[:] = [live.live]

            def _set_label(text: str) -> None:
                live.set_label(text)

            event_bus = synapse._container.resolve(EventBus)
            if event_bus is not None:
                async def _on_progress(event):
                    msg = event.message
                    # Reset streamed text at the start of each LLM call so the
                    # panel always shows the current turn.
                    if event.phase == "calling_llm":
                        # Snapshot session total before this request so streamed
                        # usage increments from this baseline (smooth counter).
                        baseline["in"] = tokens["input"]
                        baseline["out"] = tokens["output"]
                        live.reset_text()
                        live.set_label("Working...")
                        return
                    # Parse "tokens=A+B" emitted by the planning loop.
                    if msg.startswith("tokens="):
                        try:
                            a, b = msg[7:].split("+", 1)
                            tokens["input"] = int(a)
                            tokens["output"] = int(b)
                            live.set_label("Working...")
                            return
                        except (ValueError, IndexError):
                            pass
                    live.set_label(msg)
                async def _on_token(event):
                    live.add_text(event.text)
                    if event.usage:
                        u_in = event.usage.get("input", 0) or 0
                        u_out = event.usage.get("output", 0) or 0
                        tokens["input"] = baseline["in"] + u_in
                        tokens["output"] = baseline["out"] + u_out
                async def _on_tool_started(event):
                    live.set_label(f"{event.tool_name} ...")
                async def _on_tool_completed(event):
                    icon = "ok" if event.success else "FAIL"
                    live.set_label(f"{event.tool_name} [{icon}] ({event.duration_ms}ms)")
                event_bus.subscribe("agent_progress", _on_progress)
                event_bus.subscribe("llm_token", _on_token)
                event_bus.subscribe("tool_call_started", _on_tool_started)
                event_bus.subscribe("tool_call_completed", _on_tool_completed)
                swarm_tracker = _SwarmTracker(live.set_swarm_lines)
                swarm_tracker.wire(event_bus)

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
                live.stop()
                console.print(f"  [bold red]{_friendly_error(exc)}[/bold red]")
            else:
                print(_friendly_error(exc))
            continue
        finally:
            if use_rich:
                try:
                    live.stop()
                except Exception:
                    pass
                status_holder[:] = []
                if event_bus is not None:
                    try:
                        event_bus.unsubscribe("agent_progress", _on_progress)
                        event_bus.unsubscribe("llm_token", _on_token)
                        event_bus.unsubscribe("tool_call_started", _on_tool_started)
                        event_bus.unsubscribe("tool_call_completed", _on_tool_completed)
                        swarm_tracker.unwire(event_bus)
                    except Exception:
                        pass

        _print_result(console, result, use_rich)


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
