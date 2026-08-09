"""Regression tests for the live token/time display (_LiveDisplay).

These lock in the fix for the "panel freezes then jumps" bug: the refresh
cadence is owned by a background thread (independent of the asyncio event
loop) and the rendered transcript is bounded so redraws stay O(1) even during
a long generation/loop.
"""

import io

from rich.console import Console

from synapse.adapters.cli_render import _LiveDisplay


def _make_display():
    console = Console(file=io.StringIO(), force_terminal=True, width=80)
    return _LiveDisplay(console, lambda: "1.2k", lambda: "3s")


def test_buffer_is_capped():
    """add_text must drop oldest chunks once the transcript exceeds the cap,
    so _render stays O(1) and never lets the redraw thread stall."""
    disp = _make_display()
    disp.start()
    for _ in range(100):
        disp.add_text("y" * 100)  # 10000 chars total, cap is 4000
    disp.stop()
    assert disp._buf_len <= _LiveDisplay._MAX_BUF_CHARS + 100
    # oldest chunks were dropped, not all 100 retained
    assert len(disp._buf) < 100


def test_stop_joins_refresh_thread():
    """stop() must terminate the owned refresh thread without hanging, even
    right after start (the thread is the thing that keeps the clock alive)."""
    disp = _make_display()
    disp.start()
    disp.add_text("hello")
    disp.stop()
    assert not disp._thread.is_alive()
