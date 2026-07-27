"""Tests for ReActPlanner._log render routing.

_log must stay silent on stderr while the CLI is rendering progress via the
event bus (rich live panel), and otherwise print a clean line WITHOUT the old
ugly ``[Synapse]`` prefix.
"""

import io
import sys

from synapse.modules.planning.react import ReActPlanner


class _FakeBus:
    def __init__(self, subscribed: bool):
        self._subscribed = subscribed

    def has_subscribers(self, event_type: str) -> bool:
        return self._subscribed


def _capture_stderr(fn):
    buf = io.StringIO()
    old = sys.stderr
    sys.stderr = buf
    try:
        fn()
    finally:
        sys.stderr = old
    return buf.getvalue()


def test_log_silent_when_bus_rendering():
    planner = ReActPlanner()
    planner._event_bus = _FakeBus(subscribed=True)

    out = _capture_stderr(lambda: planner._log("some progress"))
    assert out == ""


def test_log_prints_clean_line_when_no_renderer():
    planner = ReActPlanner()
    planner._event_bus = _FakeBus(subscribed=False)

    out = _capture_stderr(lambda: planner._log("some progress"))
    assert "some progress" in out
    assert "[Synapse]" not in out


def test_log_prints_clean_line_when_no_bus():
    planner = ReActPlanner()
    planner._event_bus = None

    out = _capture_stderr(lambda: planner._log("some progress"))
    assert "some progress" in out
    assert "[Synapse]" not in out
