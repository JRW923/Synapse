"""Tests for CLI result rendering helpers (status color + _print_result)."""

from synapse.adapters.cli import _status_style_for, _print_result
from synapse.protocols.planner import ResultStatus, AgentResult, ExecutionMetrics


def _result(status, output="done"):
    return AgentResult(status=status, output=output, metrics=ExecutionMetrics())


def test_status_style_mapping():
    assert _status_style_for("Thinking...") == "bright_cyan"
    assert _status_style_for("Working...") == "bright_cyan"
    assert _status_style_for("web_fetch [ok] (820ms)") == "green"
    assert _status_style_for("Task completed") == "green"
    assert _status_style_for("tool FAIL") == "red"
    assert _status_style_for("Token budget exhausted") == "red"


def test_print_result_rich():
    import io
    from rich.console import Console

    out = io.StringIO()
    console = Console(file=out, width=80)
    _print_result(console, _result(ResultStatus.SUCCESS), True)
    text = out.getvalue()
    assert "SUCCESS" in text
    assert "done" in text


def test_print_result_non_rich(capsys):
    _print_result(None, _result(ResultStatus.PARTIAL), False)
    captured = capsys.readouterr().out
    assert "[Status: partial]" in captured
    assert "done" in captured


def test_live_display_is_transient():
    """A stopped panel must erase itself so panels never stack up."""
    import io
    from rich.console import Console
    from synapse.adapters.cli import _LiveDisplay

    live = _LiveDisplay(Console(file=io.StringIO(), width=80),
                        lambda: "1.2k", lambda: "12s")
    assert live._live.transient is True


def test_live_display_restartable():
    """Confirm pause/resume (stop then start) never raises — the refresh
    thread is recreated each start, so a Thread isn't started twice."""
    import io
    from rich.console import Console
    from synapse.adapters.cli import _LiveDisplay

    live = _LiveDisplay(Console(file=io.StringIO(), width=80),
                        lambda: "1.2k", lambda: "12s")
    live.start()
    live.stop()
    live.start()  # must not raise "threads can only be started once"
    live.stop()

