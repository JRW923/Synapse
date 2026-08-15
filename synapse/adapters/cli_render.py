"""Terminal rendering layer for the Synapse CLI.

Holds everything that only knows how to *draw*: the brand palette, display-cell
text measurement, and the Rich live-panel classes that turn EventBus events
into a streaming terminal view. ``cli.py`` owns argument parsing, the REPL and
the subcommands, and imports from here — the dependency is one-directional so
neither module can grow an import cycle.
"""

import contextlib
import logging
import threading
import time as _time

from synapse.core.events import EventBus
from synapse.modules.planning.react import summarize_params

_log = logging.getLogger(__name__)

#: Brand palette — single place to tweak the CLI look.
_BRAND = "bright_cyan"          # prompt, field icons and current activity
_INFO = "white"                 # stable homepage values
_LABEL = "bold bright_blue"     # all field labels
_BORDER = "bright_cyan"         # colored outer frame and separators
_HINT = "grey70"                # secondary text / hints
_SYSTEM = "grey58"              # runtime metadata, distinct from task output
_ICON = "bright_cyan"           # one consistent icon family
_MASCOT_YELLOW = "bright_yellow"
_MASCOT_RED = "bright_red"
_MASCOT_DARK = "bright_black"
_MASCOT_TAIL = "yellow3"
_SUCCESS = "green"
_WARNING = "yellow"

# Braille spinner — animates continuously while the agent works so the panel
# never looks frozen (e.g. while the model is "thinking" before the first
# token). Unicode braille, not an emoji, so it renders in any terminal.
_SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


@contextlib.contextmanager
def _swallow(where: str):
    """Keep a drawing or teardown failure from killing a running task.

    The TUI is decoration: a broken frame must never abort work the user cares
    about. Routing the exception to the debug log is what keeps these failures
    findable instead of invisible.
    """
    try:
        yield
    except Exception as exc:
        _log.debug("suppressed error in %s: %s", where, exc)


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


def _clamp_text_by_cell(t: "Text", limit: int) -> "Text":  # noqa: F821
    """Clamp a rich Text to *limit* display cells, keeping its styles."""
    if t.cell_len <= limit:
        return t
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


def _format_token_count(value: int | float | None) -> str:
    """Format token counters for a stable, compact terminal readout."""
    value = int(value or 0)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


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

    def __init__(self, console, fmt_tokens, fmt_elapsed, fmt_stats=None, fmt_progress=None):
        from rich.live import Live

        self._console = console
        self._fmt_tokens = fmt_tokens
        self._fmt_elapsed = fmt_elapsed
        self._fmt_stats = fmt_stats
        self._fmt_progress = fmt_progress
        self._buf: list[str] = []
        self._buf_len = 0
        self._label = "Thinking..."
        self._iteration = 0
        self._max_iterations = None
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

    def stop(self, *, persist: bool = False):
        self._stop.set()
        with _swallow("_LiveDisplay.stop: thread join"):
            self._thread.join(timeout=0.5)
        with _swallow("_LiveDisplay.stop: live stop"):
            self._live.stop()
        # Rich clears transient output on stop.  One-shot commands need a
        # durable final snapshot for piping/capture; interactive callers keep
        # the historical transient behaviour unless they opt in explicitly.
        if persist:
            with _swallow("_LiveDisplay.stop: persist final snapshot"):
                self._console.print(self._render())

    def _refresh_loop(self):
        """Force a screen write every ~0.2s on a thread independent of the
        event loop, so the clock/token readouts AND the spinner stay live even
        while the agent loop is busy streaming or executing tools."""
        while not self._stop.is_set():
            self._stop.wait(0.2)
            with _swallow("_LiveDisplay._refresh_loop"):
                with self._lock:
                    self._spin = (self._spin + 1) % len(_SPINNER)
                # Regenerate the renderable so the spinner frame advances, then
                # write it to the screen.
                self._live.update(self._render(), refresh=True)

    def set_label(self, text: str) -> None:
        with self._lock:
            self._label = text
        self._refresh(force=True)

    def set_iteration(self, current: int, maximum: int | None = None) -> None:
        with self._lock:
            self._iteration = max(0, int(current or 0))
            if maximum is not None:
                self._max_iterations = max(0, int(maximum))
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
        with _swallow("_LiveDisplay._refresh"):
            now = _time.monotonic()
            with self._lock:
                if not force and now - self._last_refresh < self._MIN_REFRESH_INTERVAL:
                    return
                self._last_refresh = now
            self._live.update(self._render())

    def _render(self):
        from rich import box
        from rich.panel import Panel
        from rich.text import Text

        with self._lock:
            body = "".join(self._buf)
            label = self._label
            spin = self._spin
            iteration = self._iteration
            max_iterations = self._max_iterations
            swarm_lines = list(self._swarm_lines)
            timeline = list(self._timeline)
        style = _status_style_for(label)
        # Spin only while in-progress (cyan); final states (ok/fail) show a
        # static dot so the panel reads as "settled" the moment it completes.
        dot = _SPINNER[spin % len(_SPINNER)] if style == _BRAND else "●"
        pieces = [f"[{style}]{dot} {label}[/{style}]"]
        if iteration:
            maximum = str(max_iterations) if max_iterations else "--"
            pieces.append(f"[{_SYSTEM}]iter {iteration:02d}/{maximum}[/{_SYSTEM}]")
        tk = self._fmt_tokens()
        if tk and not self._fmt_stats:
            pieces.append(f"[{_SYSTEM}]{tk} tok[/{_SYSTEM}]")
        el = self._fmt_elapsed()
        if el:
            pieces.append(f"[{_SYSTEM}]{el}[/{_SYSTEM}]")
        header = "  ·  ".join(pieces)
        lines = body[-1600:].splitlines() if body else []
        line_limit = max(20, getattr(self._console, "width", 80) - 8)
        lines = [_middle(line, line_limit) for line in lines[-6:]]
        text = Text()
        if self._fmt_stats:
            text.append(self._fmt_stats() + "\n", style=_SYSTEM)
        if self._fmt_progress:
            progress = self._fmt_progress()
            if progress:
                text.append(progress + "\n", style=_SYSTEM)
        if lines:
            text.append("\n".join(lines), style="none")
        else:
            text.append("…", style=_SYSTEM)
        if timeline:
            text.append("\n\nRECENT TOOLS\n", style=f"bold {_SYSTEM}")
            for line in timeline:
                text.append(f"  {line}\n", style=_SYSTEM)
        if swarm_lines:
            text.append("\nSWARM\n", style=f"bold {_SYSTEM}")
            for line in swarm_lines:
                text.append(line + "\n", style=_SYSTEM)
        return Panel(text, title=header, border_style=_BORDER, box=box.ROUNDED, expand=True)


class _LiveRun:
    """Shared event-to-CLI state adapter for one agent run.

    `run`, the REPL, and `chat` must render the same lifecycle. Keeping the
    subscriptions here prevents one entry point from silently losing a phase
    or leaking handlers into the next request.
    """

    def __init__(self, synapse, console, status_holder=None, *, persist_final=False):
        self.synapse = synapse
        self.status_holder = status_holder
        self.persist_final = persist_final
        self.tokens = {"input": 0, "output": 0}
        self.baseline = {"in": 0, "out": 0}
        self.elapsed = {"start": _time.monotonic()}
        self.iteration = 0
        self.max_iterations = getattr(getattr(synapse, "_config", None), "planning", None)
        self.max_iterations = getattr(self.max_iterations, "max_iterations", None)
        self.live = _LiveDisplay(
            console,
            self._fmt_tokens,
            self._fmt_elapsed,
            self._fmt_token_stats,
            self._fmt_progress,
        )
        self.event_bus = None
        self.swarm_tracker = None
        self._handlers: list[tuple[str, object]] = []
        self._tool_args: dict[str, str] = {}

    def _fmt_tokens(self) -> str:
        total = self.tokens["input"] + self.tokens["output"]
        return _format_token_count(total)

    def _fmt_token_stats(self) -> str:
        return (
            f"tokens  in {_format_token_count(self.tokens['input'])} · "
            f"out {_format_token_count(self.tokens['output'])} · "
            f"total {_format_token_count(self.tokens['input'] + self.tokens['output'])} tok"
        )

    def _fmt_progress(self) -> str:
        if not self.max_iterations or not self.iteration:
            return ""
        ratio = min(1.0, self.iteration / self.max_iterations)
        filled = int(round(ratio * 24))
        return "  " + "━" * filled + "░" * (24 - filled)

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
            return "调用模型"
        if phase == "token_budget":
            return "接近 token 预算"
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
            iteration = self._iteration(message)
            if iteration:
                self.iteration = int(iteration)
                self.live.set_iteration(self.iteration, self.max_iterations)
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
        summary = summarize_params(event.tool_params)
        self._tool_args[event.tool_name] = summary
        self.live.set_label(f"执行工具 · {event.tool_name}({summary})")

    async def _on_tool_completed(self, event):
        summary = self._tool_args.pop(event.tool_name, "")
        files = len(getattr(event, "files_touched", []) or [])
        file_suffix = f" · 文件 {files}" if files else ""
        mark = "✓" if event.success else "!"
        args = _middle(summary or "-", 28)
        timeline = f"{mark} {event.tool_name:<10} {args:<28} {event.duration_ms:>5}ms{file_suffix}"
        self.live.add_timeline(timeline)
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
            self.live.stop(persist=self.persist_final)
        finally:
            if self.status_holder is not None:
                self.status_holder[:] = []
            if self.event_bus is not None:
                for event_type, handler in self._handlers:
                    with _swallow(f"_LiveRun.stop: unsubscribe {event_type}"):
                        self.event_bus.unsubscribe(event_type, handler)
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
            with _swallow(f"_SwarmTracker.unwire: {et}"):
                event_bus.unsubscribe(et, h)
