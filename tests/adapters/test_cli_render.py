"""Tests for CLI result rendering helpers (status color + _print_result)."""

from types import SimpleNamespace

from synapse.adapters.cli import _print_result
from synapse.adapters.cli_render import _status_style_for
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
    assert _status_style_for("工具失败 · shell") == "red"
    assert _status_style_for("任务完成") == "green"


def test_print_result_rich():
    import io
    from rich.console import Console

    out = io.StringIO()
    console = Console(file=out, width=80)
    _print_result(console, _result(ResultStatus.SUCCESS), True)
    text = out.getvalue()
    assert "TASK COMPLETE" in text
    assert "done" in text


def test_print_result_non_rich(capsys):
    _print_result(None, _result(ResultStatus.PARTIAL), False)
    captured = capsys.readouterr().out
    assert "[Status: partial]" in captured
    assert "done" in captured
    assert "token 0" in captured
    assert "下一步" in captured


def test_live_display_is_transient():
    """A stopped panel must erase itself so panels never stack up."""
    import io
    from rich.console import Console
    from synapse.adapters.cli_render import _LiveDisplay

    live = _LiveDisplay(Console(file=io.StringIO(), width=80),
                        lambda: "1.2k", lambda: "12s")
    assert live._live.transient is True


def test_live_display_restartable():
    """Confirm pause/resume (stop then start) never raises — the refresh
    thread is recreated each start, so a Thread isn't started twice."""
    import io
    from rich.console import Console
    from synapse.adapters.cli_render import _LiveDisplay

    live = _LiveDisplay(Console(file=io.StringIO(), width=80),
                        lambda: "1.2k", lambda: "12s")
    live.start()
    live.stop()
    live.start()  # must not raise "threads can only be started once"
    live.stop()


def test_spinner_advances_for_in_progress():
    import io
    from rich.console import Console
    from synapse.adapters.cli_render import _LiveDisplay

    live = _LiveDisplay(Console(file=io.StringIO(), width=80),
                        lambda: "1.2k", lambda: "12s")
    live.set_label("Thinking...")
    live._spin = 0
    a = live._render().title
    live._spin = 1
    b = live._render().title
    assert a != b  # spinner frame moved


def test_final_state_uses_static_dot():
    import io
    from rich.console import Console
    from synapse.adapters.cli_render import _LiveDisplay, _SPINNER

    live = _LiveDisplay(Console(file=io.StringIO(), width=80),
                        lambda: "1.2k", lambda: "12s")
    live.set_label("web_fetch [ok] (820ms)")
    title = live._render().title
    assert not any(ch in title for ch in _SPINNER)  # settled, no spinner glyph



def test_welcome_banner_aligns_with_cjk_workspace():
    """A CJK workspace path must not break the banner's right border. Row width
    is measured in display cells (CJK = 2), not code points."""
    import io
    from unittest.mock import patch

    from rich.cells import cell_len
    from rich.console import Console

    from synapse.adapters.cli import _show_welcome
    from synapse.config.schema import SynapseConfig

    console = Console(width=72, file=io.StringIO(), force_terminal=True,
                      legacy_windows=True, record=True)
    config = SynapseConfig()
    config.provider.model = "deepseek-v4-pro"
    with patch("synapse.adapters.cli.Path.cwd", return_value="D:/项目/代码/神经网络"):
        _show_welcome(console, config, "synapse.yaml")
    lines = console.export_text().splitlines()
    widths = {cell_len(l) for l in lines}
    assert widths == {72}, f"banner rows misaligned, cell widths: {sorted(widths)}"


def test_live_display_bounds_single_large_chunk():
    """A single streamed chunk larger than the panel cap must still be bounded
    (previously a >4000-char token block grew the buffer without limit)."""
    import io
    from rich.console import Console
    from synapse.adapters.cli_render import _LiveDisplay

    cap = _LiveDisplay._MAX_BUF_CHARS
    live = _LiveDisplay(Console(file=io.StringIO(), width=80), lambda: "", lambda: "")
    live.add_text("x" * (cap + 1000))
    assert live._buf_len <= cap
    assert len(live._buf) == 1


def test_live_display_bounds_and_renders_timeline():
    import io
    from rich.console import Console
    from synapse.adapters.cli_render import _LiveDisplay

    live = _LiveDisplay(Console(file=io.StringIO(), width=80), lambda: "", lambda: "")
    for i in range(10):
        live.add_timeline(f"step-{i}")

    assert live._timeline == [f"step-{i}" for i in range(5, 10)]
    rendered = live._render().renderable.plain
    assert "RECENT TOOLS" in rendered
    assert "step-9" in rendered
    assert "step-0" not in rendered


def test_live_display_coalesces_token_refreshes():
    import io
    from unittest.mock import Mock
    from rich.console import Console
    from synapse.adapters.cli_render import _LiveDisplay

    live = _LiveDisplay(Console(file=io.StringIO(), width=80), lambda: "", lambda: "")
    live._live.update = Mock()
    for _ in range(100):
        live.add_text("x")

    assert live._live.update.call_count <= 2


def test_welcome_uses_compact_layout_on_narrow_terminal():
    import io
    from types import SimpleNamespace
    from unittest.mock import patch
    from rich.cells import cell_len
    from rich.console import Console
    from synapse.adapters.cli import _WELCOME_ART, _show_welcome
    from synapse.config.schema import SynapseConfig

    console = Console(width=40, file=io.StringIO(), force_terminal=True, record=True)
    config = SynapseConfig()
    config.provider.provider = "ollama"
    config.provider.model = "qwen3.5:4b"
    session = SimpleNamespace(id="abcdef123456", messages=[object()])

    with patch("synapse.adapters.cli.Path.cwd", return_value="D:/项目/代码"):
        _show_welcome(console, config, "synapse.yaml", session)

    text = console.export_text()
    assert _WELCOME_ART[0].strip() not in text
    assert "READY" in text
    assert "abcdef12" in text
    assert {cell_len(line) for line in text.splitlines()} == {40}


def test_welcome_uses_block_logo_on_wide_terminal():
    import io
    from rich.console import Console
    from synapse.adapters.cli import _show_welcome
    from synapse.config.schema import SynapseConfig

    console = Console(width=80, file=io.StringIO(), force_terminal=True, record=True)
    config = SynapseConfig()
    config.provider.provider = "ollama"
    config.provider.model = "qwen3.5:4b"
    _show_welcome(console, config, "models.json")

    text = console.export_text()
    assert "█████" in text
    assert ".-=========-." not in text
    assert "WORKSPACE" in text
    assert "TOOLS" in text


def test_welcome_medium_terminal_reflows_to_single_column():
    import io
    from rich.cells import cell_len
    from rich.console import Console
    from synapse.adapters.cli import _show_welcome
    from synapse.config.schema import SynapseConfig

    console = Console(width=60, file=io.StringIO(), force_terminal=True, record=True)
    config = SynapseConfig()
    config.provider.provider = "ollama"
    config.provider.model = "qwen3.5:4b"
    _show_welcome(console, config, "models.json")

    text = console.export_text()
    lines = text.splitlines()
    assert {cell_len(line) for line in lines} == {60}
    assert "WORKSPACE" in text
    assert "MODEL" in text


def test_welcome_fields_share_a_stable_value_column():
    import io
    from unittest.mock import patch
    from rich.console import Console
    from synapse.adapters.cli import _show_welcome
    from synapse.config.schema import SynapseConfig

    console = Console(width=80, file=io.StringIO(), force_terminal=True, record=True)
    config = SynapseConfig()
    config.provider.provider = "ollama"
    config.provider.model = "qwen3.5:4b"

    with patch("synapse.adapters.cli.Path.cwd", return_value="D:/项目/代码"):
        _show_welcome(console, config, "models.json")

    lines = console.export_text().splitlines()
    labels = ["SYNAPSE", "WORKSPACE", "MODEL", "PLANNING", "SESSION", "CONFIG", "TOOLS"]
    positions = [next(line.index(label) for line in lines if label in line) for label in labels]
    assert len(set(positions)) == 1


def test_welcome_frame_colors_left_and_right_edges():
    import io
    from rich.console import Console
    from synapse.adapters.cli import _show_welcome
    from synapse.config.schema import SynapseConfig

    console = Console(width=60, file=io.StringIO(), force_terminal=True, record=True)
    _show_welcome(console, SynapseConfig(), "models.json")
    html = console.export_html()
    assert "│</span>" in html


def test_input_frame_is_full_width_and_labeled():
    from rich.cells import cell_len
    from synapse.adapters.cli import _input_frame

    top = _input_frame(80, top=True, rich=False)
    bottom = _input_frame(80, top=False, rich=False)
    assert "INPUT" in top
    assert cell_len(top) == 80
    assert cell_len(bottom) == 80


def test_prompt_toolkit_input_uses_a_real_frame():
    from unittest.mock import patch
    import prompt_toolkit
    from synapse.adapters.cli import _make_prompt_session

    with patch.object(prompt_toolkit, "PromptSession", return_value="session") as factory:
        assert _make_prompt_session() == "session"

    kwargs = factory.call_args.kwargs
    assert kwargs["show_frame"] is True
    assert any(name == "frame" for name, _ in kwargs["style"].style_rules)


def test_live_display_renders_token_breakdown_and_iteration():
    import io
    from rich.console import Console
    from synapse.adapters.cli_render import _LiveDisplay

    display = _LiveDisplay(
        Console(file=io.StringIO(), width=80),
        lambda: "2.0k",
        lambda: "4s",
        lambda: "tokens  in 1.2k · out 840 · total 2.0k tok",
    )
    display.set_iteration(3, 50)
    display.set_label("调用模型")
    plain = display._render().renderable.plain

    assert "iter 03/50" in display._render().title
    assert "in 1.2k" in plain
    assert "out 840" in plain
    assert "2.0k tok" in plain


def test_live_system_metadata_uses_a_distinct_neutral_style():
    import io
    from rich.console import Console
    from synapse.adapters.cli_render import _LiveDisplay, _SYSTEM

    display = _LiveDisplay(
        Console(file=io.StringIO(), width=80),
        lambda: "2.0k",
        lambda: "4s",
        lambda: "tokens  in 1.2k · out 840 · total 2.0k tok",
    )
    display.set_iteration(3, 50)
    display.add_timeline("✓ shell                         12ms")

    styles = {span.style for span in display._render().renderable.spans}
    assert _SYSTEM in styles
    assert "white" not in styles


def test_result_summary_uses_system_style():
    import io
    from rich.console import Console

    console = Console(file=io.StringIO(), width=80, force_terminal=True, record=True)
    _print_result(console, _result(ResultStatus.SUCCESS), True)
    html = console.export_html()
    assert "#949494" in html  # Rich's grey58 output, separate from task body


def test_token_count_format_is_compact_and_stable():
    from synapse.adapters.cli_render import _format_token_count

    assert _format_token_count(42) == "42"
    assert _format_token_count(1_200) == "1.2k"
    assert _format_token_count(1_200_000) == "1.2M"


# ---- /resume interactive history picker ----------------------------------


def _fake_session(sid: str, messages: list) -> object:
    """Minimal Session stand-in carrying id + messages for picker tests."""
    return SimpleNamespace(id=sid, messages=messages)


def test_preview_session_prefers_first_user_message():
    from synapse.adapters.cli import _preview_session

    s = _fake_session("abc12345", [
        SimpleNamespace(role="system", content="你是助手"),
        SimpleNamespace(role="user", content="帮我把登录页改成深色模式\n第二行"),
        SimpleNamespace(role="assistant", content="好的，我来改"),
    ])
    assert _preview_session(s, 200) == "帮我把登录页改成深色模式 第二行"


def test_preview_session_falls_back_when_no_user_message():
    from synapse.adapters.cli import _preview_session

    s = _fake_session("def67890", [
        SimpleNamespace(role="assistant", content="任务已完成"),
    ])
    assert _preview_session(s, 200) == "任务已完成"


def test_preview_session_empty_returns_placeholder():
    from synapse.adapters.cli import _preview_session

    s = _fake_session("zero0000", [])
    assert _preview_session(s, 200) == "(空会话)"


def test_pick_session_plain_returns_chosen_index(monkeypatch):
    from synapse.adapters.cli import _pick_session_plain

    sessions = [
        _fake_session("aaa11111", [SimpleNamespace(role="user", content="X")]),
        _fake_session("bbb22222", [SimpleNamespace(role="user", content="Y")]),
    ]
    monkeypatch.setattr("builtins.input", lambda _="": "2")
    assert _pick_session_plain(sessions).id == "bbb22222"


def test_pick_session_plain_empty_input_cancels(monkeypatch):
    from synapse.adapters.cli import _pick_session_plain

    sessions = [_fake_session("aaa11111", [SimpleNamespace(role="user", content="X")])]
    monkeypatch.setattr("builtins.input", lambda _="": "")
    assert _pick_session_plain(sessions) is None

