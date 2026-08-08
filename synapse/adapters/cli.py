"""CLI entry point for Synapse."""

import argparse
import asyncio
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
    import signal as _signal

    def _unix_sigint_handler(_signum: int, _frame: object) -> None:
        global _last_ctrl_c, _ctrl_c_pressed
        now = _time.monotonic()
        if now - _last_ctrl_c < 3.0:
            _os._exit(0)
        _last_ctrl_c = now
        _ctrl_c_pressed = True
        _os.write(2, "\n  再按一次 Ctrl+C 退出。\n".encode("utf-8"))

    _signal.signal(_signal.SIGINT, _unix_sigint_handler)

from synapse.config import load_config
from synapse.config.schema import _effective_api_key
from synapse.protocols.mcp import McpServerConfig
from synapse.core.agent import Agent
from synapse.modules.planning.react import _summarize_params
from synapse.core.session import Session
from synapse.protocols.planner import Planner

import signal as _signal


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
    _MAX_TIMELINE = 5
    # Coalesce token-driven redraws; the background clock still refreshes at
    # 200ms so a fast provider cannot monopolize the terminal.
    _MIN_REFRESH_INTERVAL = 0.05

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
        self._spin = 0
        self._swarm_lines: list[str] = []
        self._timeline: list[str] = []
        self._last_refresh = 0.0
        # Writers run on the event-loop thread; the refresh thread reads the
        # same state. The lock keeps redraws from seeing torn state.
        self._lock = threading.Lock()
        # auto_refresh=False: we drive screen writes from our own thread so the
        # cadence never depends on the event loop or on event-handler storms.
        self._live = Live(self._render(), console=console, auto_refresh=False,
                          transient=True)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._refresh_loop, daemon=True)

    @property
    def live(self):
        return self._live

    def start(self):
        # Recreate the refresh thread so the display can be stopped and
        # restarted (e.g. paused for a confirmation prompt) — a Thread object
        # can't be started twice, so we build a fresh one each time.
        self._live.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._refresh_loop, daemon=True)
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
        event loop, so the clock/token readouts AND the spinner stay live even
        while the agent loop is busy streaming or executing tools."""
        while not self._stop.is_set():
            self._stop.wait(0.2)
            try:
                with self._lock:
                    self._spin = (self._spin + 1) % len(_SPINNER)
                # Regenerate the renderable so the spinner frame advances, then
                # write it to the screen.
                self._live.update(self._render(), refresh=True)
            except Exception:
                pass

    def set_label(self, text: str) -> None:
        with self._lock:
            self._label = text
        self._refresh(force=True)

    def add_timeline(self, text: str) -> None:
        """Append one compact completed step, keeping the panel bounded."""
        with self._lock:
            self._timeline.append(str(text))
            if len(self._timeline) > self._MAX_TIMELINE:
                self._timeline = self._timeline[-self._MAX_TIMELINE:]
        self._refresh(force=True)

    def add_text(self, text: str) -> None:
        with self._lock:
            self._buf.append(text)
            self._buf_len += len(text)
            # Drop oldest chunks once we exceed the cap so joins stay bounded.
            while self._buf_len > self._MAX_BUF_CHARS and len(self._buf) > 1:
                dropped = self._buf.pop(0)
                self._buf_len -= len(dropped)
            # A single chunk larger than the cap must still be bounded — keep
            # its tail instead of growing without limit.
            if self._buf_len > self._MAX_BUF_CHARS:
                tail = "".join(self._buf)[-self._MAX_BUF_CHARS:]
                self._buf = [tail]
                self._buf_len = len(tail)
        self._refresh()

    def reset_text(self) -> None:
        with self._lock:
            self._buf = []
            self._buf_len = 0
        self._refresh(force=True)

    def set_swarm_lines(self, lines: list[str]) -> None:
        """Replace the swarm-status footer lines shown under the streamed text."""
        with self._lock:
            self._swarm_lines = list(lines)
        self._refresh(force=True)

    def _refresh(self, force: bool = False) -> None:
        try:
            now = _time.monotonic()
            with self._lock:
                if not force and now - self._last_refresh < self._MIN_REFRESH_INTERVAL:
                    return
                self._last_refresh = now
            self._live.update(self._render())
        except Exception:
            pass

    def _render(self):
        from rich.panel import Panel
        from rich.text import Text

        with self._lock:
            body = "".join(self._buf)
            label = self._label
            spin = self._spin
            swarm_lines = list(self._swarm_lines)
            timeline = list(self._timeline)
        style = _status_style_for(label)
        # Spin only while in-progress (cyan); final states (ok/fail) show a
        # static dot so the panel reads as "settled" the moment it completes.
        dot = _SPINNER[spin % len(_SPINNER)] if style == _BRAND else "●"
        pieces = [f"[{style}]{dot} {label}[/{style}]"]
        tk = self._fmt_tokens()
        if tk:
            pieces.append(f"[dim]{tk} tok[/dim]")
        el = self._fmt_elapsed()
        if el:
            pieces.append(f"[dim]{el}[/dim]")
        header = "  ·  ".join(pieces)
        text = Text(body, style="none") if body else Text("…", style="dim")
        if timeline:
            text.append("\n\n最近步骤\n", style="dim")
            for line in timeline:
                text.append(f"  {line}\n", style="cyan")
        if swarm_lines:
            text.append("\n\n")
            for line in swarm_lines:
                text.append(line + "\n", style="cyan")
        return Panel(text, title=header, border_style=_BORDER, expand=True)


class _LiveRun:
    """Shared event-to-CLI state adapter for one agent run.

    `run`, the REPL, and `chat` must render the same lifecycle. Keeping the
    subscriptions here prevents one entry point from silently losing a phase
    or leaking handlers into the next request.
    """

    def __init__(self, synapse, console, status_holder=None):
        self.synapse = synapse
        self.status_holder = status_holder
        self.tokens = {"input": 0, "output": 0}
        self.baseline = {"in": 0, "out": 0}
        self.elapsed = {"start": _time.monotonic()}
        self.live = _LiveDisplay(console, self._fmt_tokens, self._fmt_elapsed)
        self.event_bus = None
        self.swarm_tracker = None
        self._handlers: list[tuple[str, object]] = []
        self._tool_args: dict[str, str] = {}

    def _fmt_tokens(self) -> str:
        total = self.tokens["input"] + self.tokens["output"]
        return f"{total / 1000:.1f}k" if total >= 1000 else str(total)

    def _fmt_elapsed(self) -> str:
        seconds = _time.monotonic() - self.elapsed["start"]
        return f"{seconds:.0f}s" if seconds < 60 else f"{int(seconds // 60)}m{int(seconds % 60):02d}s"

    @staticmethod
    def _iteration(message: str) -> str:
        prefix = "Iteration "
        if message.startswith(prefix):
            value = message[len(prefix):].split(":", 1)[0].strip()
            if value.isdigit():
                return value
        return ""

    def _progress_label(self, event) -> str:
        phase = getattr(event, "phase", "")
        message = getattr(event, "message", "") or ""
        if phase == "thinking":
            return "分析任务"
        if phase == "calling_llm":
            iteration = self._iteration(message)
            return f"调用模型 · 第 {iteration} 轮" if iteration else "调用模型"
        if phase == "token_budget":
            return f"接近 token 预算 · {message}"
        if phase == "context_timeout":
            return "上下文检索超时"
        if phase == "done":
            return "任务完成"
        return message or phase or "处理中"

    async def _on_progress(self, event):
        message = getattr(event, "message", "") or ""
        if getattr(event, "phase", "") == "calling_llm":
            self.baseline["in"] = self.tokens["input"]
            self.baseline["out"] = self.tokens["output"]
            self.live.reset_text()
            self.live.set_label(self._progress_label(event))
            return
        if message.startswith("tokens="):
            try:
                input_tokens, output_tokens = message[7:].split("+", 1)
                self.tokens["input"] = int(input_tokens)
                self.tokens["output"] = int(output_tokens)
                self.live._refresh(force=True)
                return
            except (ValueError, IndexError):
                pass
        self.live.set_label(self._progress_label(event))

    async def _on_token(self, event):
        self.live.add_text(event.text)
        if event.usage:
            self.tokens["input"] = self.baseline["in"] + (event.usage.get("input", 0) or 0)
            self.tokens["output"] = self.baseline["out"] + (event.usage.get("output", 0) or 0)

    async def _on_tool_started(self, event):
        summary = _summarize_params(event.tool_params)
        self._tool_args[event.tool_name] = summary
        self.live.set_label(f"执行工具 · {event.tool_name}({summary})")

    async def _on_tool_completed(self, event):
        status = "ok" if event.success else "失败"
        summary = self._tool_args.pop(event.tool_name, "")
        suffix = f"({summary})" if summary else ""
        files = len(getattr(event, "files_touched", []) or [])
        file_suffix = f" · 文件 {files}" if files else ""
        self.live.add_timeline(f"[{status}] {event.tool_name}{suffix} · {event.duration_ms}ms{file_suffix}")
        self.live.set_label(
            f"工具完成 · {event.tool_name}" if event.success else f"工具失败 · {event.tool_name}"
        )

    def start(self) -> None:
        self.live.start()
        if self.status_holder is not None:
            self.status_holder[:] = [self.live]
        self.event_bus = self.synapse._container.resolve(EventBus)
        if self.event_bus is None:
            return
        for event_type, handler in (
            ("agent_progress", self._on_progress),
            ("llm_token", self._on_token),
            ("tool_call_started", self._on_tool_started),
            ("tool_call_completed", self._on_tool_completed),
        ):
            self.event_bus.subscribe(event_type, handler)
            self._handlers.append((event_type, handler))
        self.swarm_tracker = _SwarmTracker(self.live.set_swarm_lines)
        self.swarm_tracker.wire(self.event_bus)

    def stop(self) -> None:
        try:
            self.live.stop()
        finally:
            if self.status_holder is not None:
                self.status_holder[:] = []
            if self.event_bus is not None:
                for event_type, handler in self._handlers:
                    try:
                        self.event_bus.unsubscribe(event_type, handler)
                    except Exception:
                        pass
                if self.swarm_tracker is not None:
                    self.swarm_tracker.unwire(self.event_bus)
            self._handlers.clear()


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
    live_run = _LiveRun(synapse, console, status_holder)
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
    chat_parser.add_argument(
        "--resume",
        nargs="?", const="__latest__", default=None, metavar="SESSION_ID",
        help="Resume a saved session by id (omit the value to resume the most "
             "recent session).",
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
        try:
            config, _ = load_config()
        except Exception as exc:
            print(_friendly_error(exc))
            return
        _check_api_key(config)

        from synapse.adapters.library import Synapse

        kwargs: dict[str, object] = {
            "provider": args.provider or config.provider.provider,
            "model": args.model or config.provider.model,
            "memory_backend": args.memory_backend,
            "enable_external_tools": args.enable_external_tools,
        }
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
        status_holder: list = []
        prev_handler = _install_cancel_handler(synapse, status_holder)
        try:
            result = asyncio.run(
                _run_task_streamed(synapse, task, session, console, use_rich, status_holder)
            )
        except KeyboardInterrupt:
            try:
                session.save()
            except Exception:
                pass
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

        from synapse.adapters.library import Synapse
        from synapse.adapters.server import create_app

        try:
            config, _ = load_config()
        except Exception as exc:
            print(_friendly_error(exc))
            return
        if args.provider:
            config.provider.provider = args.provider
        if args.model:
            config.provider.model = args.model

        synapse = Synapse(
            provider=config.provider.provider,
            model=config.provider.model,
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
    ("/provider",        "显示/切换供应商"),
    ("/mode",            "切换规划模式"),
    ("/tools",           "列出可用工具"),
    ("/context-report",  "上下文区块引用热力图"),
    ("/score",           "运行时评分 + 过程提示"),
    ("/todos",           "查看当前任务清单"),
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
        )
    except Exception:
        # No console (e.g. stdin piped, or a non-console host) — degrade to
        # plain input() instead of crashing startup.
        return None


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

# Braille spinner — animates continuously while the agent works so the panel
# never looks frozen (e.g. while the model is "thinking" before the first
# token). Unicode braille, not an emoji, so it renders in any terminal.
_SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def _status_style_for(label: str) -> str:
    """Pick a Rich style for a live-panel status label."""
    low = (label or "").lower()
    if ("fail" in low or "error" in low or "budget" in low or "denied" in low
            or "失败" in low or "错误" in low or "预算" in low or "拒绝" in low):
        return "red"
    if ("ok" in low or "completed" in low or "done" in low
            or "完成" in low):
        return "green"
    return _BRAND  # cyan — in-progress / neutral activity


def _result_summary(result) -> str:
    metrics = result.metrics
    total_tokens = metrics.tokens_input + metrics.tokens_output
    return (
        f"耗时 {metrics.duration_ms / 1000:.1f}s · token {total_tokens} · "
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
    from rich.markdown import Markdown
    from rich.rule import Rule
    color = {"success": "green", "partial": "yellow", "failed": "red"}.get(
        result.status.value, "dim"
    )
    console.print()
    console.print(Rule(style=_BORDER))
    console.print(f"  [{color}]● {result.status.value.upper()}[/{color}]")
    console.print(Markdown(result.output))
    console.print(f"  [{_HINT}]{summary}[/{_HINT}]")
    if hint:
        console.print(f"  [{_HINT}]下一步：{hint}[/{_HINT}]")
    console.print()


def _cell_len(text) -> int:
    """Display width of *text* (CJK counts 2 cells)."""
    from rich.cells import cell_len
    return cell_len(str(text))


def _clamp_by_cell(text: str, limit: int) -> str:
    """Truncate *text* to at most *limit* display cells without splitting a wide char."""
    if _cell_len(text) <= limit:
        return text
    out: list[str] = []
    w = 0
    for ch in text:
        c = _cell_len(ch)
        if w + c > limit:
            break
        out.append(ch)
        w += c
    return "".join(out)


def _clamp_text_by_cell(t: "Text", limit: int) -> "Text":
    """Clamp a rich Text to *limit* display cells, keeping its styles."""
    if t.cell_len <= limit:
        return t
    from rich.text import Text
    idx, w = 0, 0
    for i, ch in enumerate(t.plain):
        c = _cell_len(ch)
        if w + c > limit:
            break
        idx = i + 1
        w += c
    return t[:idx]


def _middle(text: str, limit: int) -> str:
    """Truncate with ellipsis in the middle if too long (measured in display cells)."""
    text = str(text).replace("\n", " ")
    if _cell_len(text) <= limit:
        return text
    if limit <= 3:
        return _clamp_by_cell(text, limit)
    left = (limit - 3) // 2
    right = limit - 3 - left
    return _clamp_by_cell(text, left) + "..." + _clamp_by_cell(text[::-1], right)[::-1]


def _show_welcome(console, config, config_path: str = "", session=None):
    """pico-style boxed welcome banner with Rich colour accents."""
    from synapse import __version__
    from rich.text import Text

    provider = config.provider.provider
    model = config.provider.model
    cwd = str(Path.cwd())
    available, _ = _available_models(config)
    ready = any(e.provider == provider and e.model == model for e in available)
    status = "ready" if ready else "needs config"
    session_label = "new"
    if session is not None:
        session_label = f"{session.id[:8]} · {len(session.messages)} msgs"

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
        """Center *body* in the box by display cell width (CJK-safe)."""
        stripped = body.strip()
        content = _middle(stripped, inner) if stripped else ""
        pad = max(0, inner - _cell_len(content))
        left_pad = pad // 2
        t = Text("| ")
        t.append(" " * left_pad)
        t.append(content, style=style)
        t.append(" " * (pad - left_pad))
        t.append(" |")
        return t

    def _row(*segments) -> None:
        """Print one boxed row from styled Text segments (CJK-safe padding)."""
        line = Text("| ")
        vis = 0
        for seg in segments:
            line.append(seg)
            vis += _cell_len(seg)
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
        """Two-column row with aligned label columns (CJK-safe)."""
        if width < 72:
            _row(_clamp_text_by_cell(_field(l_label, l_icon, l_val), inner))
            _row(_clamp_text_by_cell(_field(r_label, r_icon, r_val), inner))
            return
        l_field = _clamp_text_by_cell(_field(l_label, l_icon, l_val), left_w)
        r_field = _clamp_text_by_cell(_field(r_label, r_icon, r_val), right_w)
        l_pad = max(0, left_w - l_field.cell_len)
        r_pad = max(0, right_w - r_field.cell_len)
        _row(
            Text.assemble(l_field, " " * l_pad),
            Text(" " * gap),
            Text.assemble(r_field, " " * r_pad),
        )

    # ── render ────────────────────────────────────────────────────────
    console.print(_b("="))
    if width >= 72:
        for art_line in _WELCOME_ART:
            console.print(_centered(art_line, style=_BRAND))
    # Name · subtitle · status on one line
    tagline_plain = f"{_WELCOME_NAME}  |  {_WELCOME_SUBTITLE}  |  {status}"
    tagline_body = _middle(tagline_plain, inner).center(inner)
    tagline_rich = (
        tagline_body
        .replace(_WELCOME_NAME, f"[bold {_BRAND}]{_WELCOME_NAME}[/bold {_BRAND}]")
        .replace(_WELCOME_SUBTITLE, f"[dim italic]{_WELCOME_SUBTITLE}[/dim italic]")
        .replace(status, f"[{'green' if ready else 'yellow'}]{status}[/{'green' if ready else 'yellow'}]")
    )
    console.print(f"| {tagline_rich} |")
    console.print(_b("-"))
    _row(Text(""))

    # Workspace row
    ws = _field("WORKSPACE", ">", cwd)
    _row(_clamp_text_by_cell(ws, inner))

    _pair("MODEL", "*", model, "VERSION", "#", f"v{__version__}")
    _pair("PROVIDER", "@", provider, "PLANNING", "~", config.planning.mode)
    _pair("SESSION", "&", session_label, "STATUS", "!", status)
    if config_path:
        cfg = Text()
        cfg.append("% ", style=_BRAND)
        cfg.append(_middle(f"config  {config_path}", inner - 2), style=_HINT)
        _row(cfg)

    _row(Text(""))
    console.print(_centered("输入 /help 查看命令", style=_HINT))
    console.print(_b("="))


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
    t.add_row("  /model [name|num]", "显示/切换模型（数字快速选择）")
    t.add_row("  /provider [name]", "显示/切换供应商")
    t.add_row("  /mode [name]", "规划模式 (react / plan_execute / hierarchical / swarm)")
    t.add_row("  /tools", "列出可用工具")
    t.add_row("  /context-report", "上下文区块引用 / 使用热力图")
    t.add_row("  /score", "运行时评分 (safety/process/quality/efficiency) + 提示")
    t.add_row("  /todos", "查看当前任务清单")
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
        key = cp.api_key or main_key
        for model_name in cp.models:
            entry = ModelEntry(provider=cp.name, model=model_name, api_key=cp.api_key, base_url=cp.base_url)
            if key or cp.api_key:
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
    import yaml
    out_path.write_text(
        "# Synapse config — auto-generated by first-run wizard\n"
        + yaml.safe_dump(
            {"provider": {"provider": provider, "model": model, "api_key": api_key}},
            allow_unicode=True,
            sort_keys=False,
        ),
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

    # First-run wizard: the selected model must be usable; an unrelated key (or
    # the always-keyless Ollama preset) must not suppress setup.
    avail, _ = _available_models(config)
    current_ready = any(
        e.provider == config.provider.provider and e.model == config.provider.model
        for e in avail
    )
    if not current_ready:
        if use_rich:
            _first_run_wizard(console, config)
        else:
            _first_run_wizard_plain(config)
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

    # Show the welcome banner immediately, before heavy imports.
    if use_rich:
        _show_welcome(console, config, config_source, session)
        _last_cols = console.width
    else:
        print(f"输入任务开始工作，输入 /help 查看命令\n")

    # Deferred — created on first user input.
    _synapse: object = None
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
            _show_welcome(console, config, config_source, session)

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
                # SESSION memory outlives the Session object — clear it too so
                # prior tasks' summaries don't leak into the next task.
                if _synapse is not None:
                    try:
                        getattr(_synapse, "clear_session_memory", lambda: None)()
                    except Exception:
                        pass
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
                    idx = _pick_model(console, pick_entries, initial=cur_idx)
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
                try:
                    session.save()
                except Exception:
                    pass
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
