"""Tests for the CLI streaming helper (TODO L.1)."""

import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock

from synapse.protocols.planner import ResultStatus, AgentResult, ExecutionMetrics
from synapse.core.events import EventBus
from synapse.protocols.events import (
    AgentProgress,
    LLMToken,
    ToolCallStarted,
    ToolCallCompleted,
)


def test_eval_config_dataset_identity_is_path_free(tmp_path):
    from synapse.adapters.cli import _build_eval_config

    left = tmp_path / "left" / "tasks.jsonl"
    right = tmp_path / "right" / "tasks.jsonl"
    left.parent.mkdir()
    right.parent.mkdir()
    left.write_text('{"id":"one"}\n', encoding="utf-8")
    right.write_bytes(left.read_bytes())

    def config_for(path):
        args = SimpleNamespace(
            benchmark="terminal_bench",
            repeat=2,
            max_tasks=1,
            dataset=str(path),
            dataset_version="v1",
            dataset_source="fixture",
            dataset_license="MIT",
            workspace=None,
        )
        return _build_eval_config(args, "openai", "fixture-model", "temporary")

    first = config_for(left)
    second = config_for(right)
    assert first == second
    assert str(tmp_path) not in json.dumps(first)


def test_workspace_identity_changes_with_repo_state_without_leaking_path(tmp_path):
    from synapse.adapters.cli import _workspace_identity

    try:
        subprocess.run(["git", "--version"], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git is unavailable")
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "eval@example.test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Eval"], cwd=root, check=True)
    source = root / "value.txt"
    source.write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)

    clean = _workspace_identity(root)
    source.write_text("two\n", encoding="utf-8")
    dirty = _workspace_identity(root)

    assert clean["kind"] == "git"
    assert clean["state_sha256"] != dirty["state_sha256"]
    assert dirty["dirty"] is True
    assert str(root) not in json.dumps(dirty)


@pytest.mark.asyncio
async def test_swebench_cli_rejects_host_execution_before_runner(tmp_path, monkeypatch):
    from synapse.adapters.cli import _run_swebench_eval
    from synapse.eval.runner import BenchmarkRunner

    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text(
        json.dumps({
            "instance_id": "one",
            "problem_statement": "Fix it",
            "repo": "example/repo",
            "base_commit": "deadbeef",
        }) + "\n",
        encoding="utf-8",
    )

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("benchmark runner must not start")

    monkeypatch.setattr(BenchmarkRunner, "run", must_not_run)
    args = SimpleNamespace(
        dataset=str(dataset),
        max_tasks=None,
        repeat=1,
        trusted_host_execution=False,
    )

    with pytest.raises(RuntimeError, match="trusted_host_execution=True"):
        await _run_swebench_eval(args, "openai", "fixture-model", tmp_path / "report.json")


@pytest.mark.asyncio
async def test_terminal_cli_rejects_host_grader_before_runner(tmp_path, monkeypatch):
    from synapse.adapters.cli import _run_terminal_eval
    from synapse.eval.runner import BenchmarkRunner

    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text(
        json.dumps({
            "task_id": "one",
            "instruction": "Create marker.txt",
            "grader_command": ["python", "-c", "raise SystemExit(0)"],
        }) + "\n",
        encoding="utf-8",
    )

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("benchmark runner must not start")

    monkeypatch.setattr(BenchmarkRunner, "run", must_not_run)
    args = SimpleNamespace(
        benchmark="terminal_bench",
        dataset=str(dataset),
        max_tasks=None,
        repeat=1,
        trusted_host_execution=False,
    )

    with pytest.raises(RuntimeError, match="trusted_host_execution=True"):
        await _run_terminal_eval(
            args, "openai", "fixture-model", tmp_path / "report.json",
        )


@pytest.mark.asyncio
async def test_experiment_cli_supplies_comparability_evidence(tmp_path, monkeypatch):
    from synapse.adapters.cli import _run_experiment
    import synapse.adapters.library as library

    class FakeSynapse:
        def __init__(self, **config):
            self.config = config

        async def run(self, _task):
            return AgentResult(
                status=ResultStatus.SUCCESS,
                output="done",
                metrics=ExecutionMetrics(
                    duration_ms=10,
                    tokens_input=4,
                    tokens_output=2,
                    tool_call_count=1,
                    tool_success_count=1,
                ),
            )

        def get_run_score(self):
            return {"model_id": "fixture-model", "safety": {}}

        def get_effective_config(self):
            return {
                "variant": self.config["variant"],
                "provider": {"max_tokens": 100, "timeout_seconds": 30},
                "planning": {
                    "max_iterations": 5,
                    "max_tokens_per_task": 1000,
                    "total_timeout_seconds": 60,
                },
                "context": {"total_tokens": 500},
                "security": {
                    "sandbox_enabled": True,
                    "sandbox_mode": "enforce",
                    "sandbox_backend": "docker",
                    "sandbox_network": False,
                    "auth_confirmation": True,
                    "allowed_paths": [],
                    "allow_external": False,
                },
                "tools": {"enabled": ["read"], "allowlist_commands": []},
                "runtime": {"enable_external_tools": False, "mcp_servers": []},
            }

    monkeypatch.setattr(library, "Synapse", FakeSynapse)
    report = tmp_path / "experiment.json"
    args = SimpleNamespace(
        name="contract",
        config_a='{"variant":"A"}',
        config_b='{"variant":"B"}',
        task="diagnostic task",
        runs=2,
        primary_metric="duration_ms",
        direction="lower",
        seed=7,
        allowed_config_diff=["variant"],
        report=str(report),
    )

    await _run_experiment(args)

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["comparability_eligible"] is True
    assert payload["comparability_issues"] == []
    assert payload["comparability_evidence"]["workspace_instances"] == 4


@pytest.mark.asyncio
async def test_experiment_cli_runs_multitask_dataset_with_external_grader(
    tmp_path, monkeypatch,
):
    from synapse.adapters.cli import _run_experiment
    import synapse.adapters.library as library

    class FakeSynapse:
        def __init__(self, **config):
            self.config = config

        async def run(self, task, **_kwargs):
            (Path(self.config["workspace_root"]) / f"{task}.txt").write_text(
                "ok", encoding="utf-8",
            )
            return AgentResult(
                status=ResultStatus.SUCCESS,
                output="done",
                metrics=ExecutionMetrics(
                    duration_ms=10,
                    tokens_input=4,
                    tokens_output=2,
                    tool_call_count=1,
                    tool_success_count=1,
                ),
            )

        def get_run_score(self):
            return {"model_id": "fixture-model", "safety": {}}

        def get_effective_config(self):
            return {
                "variant": self.config["variant"],
                "provider": {"max_tokens": 100, "timeout_seconds": 30},
                "planning": {
                    "max_iterations": 5,
                    "max_tokens_per_task": 1000,
                    "total_timeout_seconds": 60,
                    "max_tool_result_chars": 1000,
                },
                "context": {"total_tokens": 500},
                "security": {
                    "sandbox_enabled": True,
                    "sandbox_mode": "enforce",
                    "sandbox_backend": "docker",
                    "sandbox_network": False,
                    "auth_confirmation": True,
                    "allowed_paths": [],
                    "allow_external": False,
                },
                "tools": {"enabled": ["read", "write"], "allowlist_commands": []},
                "runtime": {"enable_external_tools": False, "mcp_servers": []},
            }

    monkeypatch.setattr(library, "Synapse", FakeSynapse)
    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text(
        "\n".join([
            json.dumps({
                "id": "one", "instruction": "one",
                "expected_files": {"one.txt": "ok"},
            }),
            json.dumps({
                "id": "two", "instruction": "two",
                "expected_files": {"two.txt": "ok"},
            }),
        ]),
        encoding="utf-8",
    )
    report = tmp_path / "multitask.json"
    args = SimpleNamespace(
        name="multitask",
        config_a='{"variant":"A"}',
        config_b='{"variant":"B"}',
        task="unused",
        dataset=str(dataset),
        max_tasks=None,
        trusted_host_execution=False,
        runs=1,
        primary_metric=None,
        direction=None,
        seed=7,
        allowed_config_diff=["variant"],
        report=str(report),
    )

    await _run_experiment(args)

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["primary_metric"] == "functional_success"
    assert payload["task_count"] == 2
    assert payload["attempt_pairs"] == 2
    assert payload["metric_coverage"]["functional_success"]["complete_tasks"] == 2
    assert payload["comparability_eligible"] is True
    assert report.with_suffix(".html").exists()


@pytest.mark.asyncio
async def test_experiment_cli_rejects_dataset_with_passing_baseline(
    tmp_path, monkeypatch,
):
    from synapse.adapters.cli import _run_experiment
    import synapse.adapters.library as library

    class FakeSynapse:
        def __init__(self, **config):
            self.config = config

        async def run(self, _task, **_kwargs):
            raise AssertionError("Agent must not run before dataset preflight")

        def get_effective_config(self):
            return {"variant": self.config["variant"]}

    monkeypatch.setattr(library, "Synapse", FakeSynapse)
    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text(
        json.dumps({
            "id": "already-green",
            "instruction": "do nothing",
            "setup_files": {"marker.txt": "ok"},
            "expected_files": {"marker.txt": "ok"},
        }) + "\n",
        encoding="utf-8",
    )
    args = SimpleNamespace(
        name="invalid-baseline",
        config_a='{"variant":"A"}',
        config_b='{"variant":"B"}',
        task="unused",
        dataset=str(dataset),
        max_tasks=None,
        trusted_host_execution=False,
        runs=1,
        primary_metric=None,
        direction=None,
        seed=7,
        allowed_config_diff=["variant"],
        report=str(tmp_path / "must-not-exist.json"),
    )

    with pytest.raises(RuntimeError, match="baseline already passes"):
        await _run_experiment(args)


@pytest.mark.asyncio
async def test_run_task_streamed_non_rich_falls_back():
    """Without rich, the helper simply runs the task and returns the result."""
    from synapse.adapters.cli import _run_task_streamed

    mock = AsyncMock()
    mock.run.return_value = AgentResult(
        status=ResultStatus.SUCCESS, output="ok", metrics=ExecutionMetrics(),
    )
    result = await _run_task_streamed(mock, "t", None, None, False)
    assert result.status.value == "success"
    mock.run.assert_called_once()


@pytest.mark.asyncio
async def test_run_task_streamed_rich_streams_and_cleans_up():
    """With rich, the helper subscribes to the bus, runs, and unsubscribes."""
    from synapse.adapters.cli import _run_task_streamed
    from rich.console import Console

    mock = AsyncMock()
    mock.run.return_value = AgentResult(
        status=ResultStatus.SUCCESS, output="hi", metrics=ExecutionMetrics(),
    )

    class _Container:
        def resolve(self, _t):
            return EventBus()

    mock._container = _Container()
    bus = mock._container.resolve(EventBus)

    result = await _run_task_streamed(mock, "t", None, Console(), True)
    assert result.status.value == "success"
    # After the run, no handlers should remain subscribed (cleanup ran).
    # _handlers is a defaultdict, so assert every bucket is empty rather than
    # comparing to {} (empty keys persist).
    assert all(len(v) == 0 for v in bus._handlers.values())


@pytest.mark.asyncio
async def test_run_task_streamed_increments_tokens_from_stream():
    """End-to-end: streamed per-chunk usage must tick the token counter up
    (12 = 10 in + 2 out) instead of staying at 0 until the final 'tokens='."""
    from synapse.adapters.cli import _run_task_streamed
    from rich.console import Console

    bus = EventBus()

    class _Container:
        def resolve(self, _t):
            return bus

    async def _run(task, session=None):
        await bus.emit(AgentProgress(session_id="s", phase="calling_llm", message="calling"))
        await bus.emit(LLMToken(session_id="s", text="Hi", usage={"input": 10, "output": 1}))
        await bus.emit(LLMToken(session_id="s", text=" there", usage={"input": 10, "output": 2}))
        # Authoritative reconciliation at end of request.
        await bus.emit(AgentProgress(session_id="s", phase="token_update", message="tokens=10+2"))
        return AgentResult(status=ResultStatus.SUCCESS, output="done", metrics=ExecutionMetrics())

    class _Syn:
        _container = _Container()
        run = staticmethod(_run)

    console = Console(file=io.StringIO(), force_terminal=True, width=80)
    result = await _run_task_streamed(_Syn(), "t", None, console, True)
    assert result.status.value == "success"
    assert "12 tok" in console.file.getvalue()


@pytest.mark.asyncio
async def test_run_task_streamed_resets_baseline_per_request():
    """A second request must increment from the first request's total (no
    double-count, no reset to zero)."""
    from synapse.adapters.cli import _run_task_streamed
    from rich.console import Console

    bus = EventBus()

    class _Container:
        def resolve(self, _t):
            return bus

    async def _run(task, session=None):
        # request 1: 10 in + 2 out -> 12
        await bus.emit(AgentProgress(session_id="s", phase="calling_llm", message="c1"))
        await bus.emit(LLMToken(session_id="s", text="a", usage={"input": 10, "output": 2}))
        await bus.emit(AgentProgress(session_id="s", phase="token_update", message="tokens=10+2"))
        # request 2: 5 in + 3 out -> baseline(12) + 8 = 20
        await bus.emit(AgentProgress(session_id="s", phase="calling_llm", message="c2"))
        await bus.emit(LLMToken(session_id="s", text="b", usage={"input": 5, "output": 3}))
        await bus.emit(AgentProgress(session_id="s", phase="token_update", message="tokens=15+5"))
        return AgentResult(status=ResultStatus.SUCCESS, output="done", metrics=ExecutionMetrics())

    class _Syn:
        _container = _Container()
        run = staticmethod(_run)

    console = Console(file=io.StringIO(), force_terminal=True, width=80)
    await _run_task_streamed(_Syn(), "t", None, console, True)
    # Final authoritative total is 15 in + 5 out = 20; counter must reach it
    # via baseline + streamed usage, not reset per request.
    assert "20 tok" in console.file.getvalue()


@pytest.mark.asyncio
async def test_run_task_streamed_shows_phase_and_tool_timeline():
    from synapse.adapters.cli import _run_task_streamed
    from rich.console import Console

    bus = EventBus()

    class _Container:
        def resolve(self, _t):
            return bus

    async def _run(task, session=None):
        await bus.emit(AgentProgress(
            session_id="s", phase="calling_llm", message="Iteration 2: calling LLM...",
        ))
        await bus.emit(ToolCallStarted(
            session_id="s", tool_name="read", tool_params={"path": "src/app.py"},
        ))
        await bus.emit(ToolCallCompleted(
            session_id="s", tool_name="read", success=True, duration_ms=23,
            files_touched=[],
        ))
        return AgentResult(
            status=ResultStatus.SUCCESS, output="done", metrics=ExecutionMetrics(),
        )

    class _Syn:
        _container = _Container()
        run = staticmethod(_run)

    console = Console(file=io.StringIO(), force_terminal=True, width=80)
    await _run_task_streamed(_Syn(), "t", None, console, True)
    rendered = console.file.getvalue()
    assert "read" in rendered
    assert "23ms" in rendered
    assert "RECENT TOOLS" in rendered


@pytest.mark.asyncio
async def test_swarm_tracker_renders_lifecycle():
    """_SwarmTracker turns swarm events into compact panel lines and cleans up."""
    from synapse.adapters.cli_render import _SwarmTracker
    from synapse.protocols.events import (
        WorkerSpawned, WorkerCompleted, ReviewSubmitted, SwarmVerified,
    )

    updates = []
    tracker = _SwarmTracker(updates.append)
    bus = EventBus()
    tracker.wire(bus)

    await bus.emit(WorkerSpawned(session_id="s1", agent_id="w1", role="coder", task="x"))
    await bus.emit(WorkerCompleted(session_id="s1", agent_id="w1", role="coder", status="success"))
    await bus.emit(ReviewSubmitted(session_id="s1", agent_id="w2", reviewer_role="reviewer",
                                   target_role="coder", verdict="reject", comments="nope"))
    await bus.emit(SwarmVerified(session_id="s1", status="partial", issues="i"))

    joined = "\n".join(tracker.render_lines())
    assert "coder" in joined
    assert "rejected=1" in joined
    assert "verified: partial" in joined
    # on_update fired once per event (spawn, complete, review, verify).
    assert len(updates) == 4

    tracker.unwire(bus)
    assert all(len(v) == 0 for v in bus._handlers.values())


def test_friendly_error_maps_synapse_errors():
    """L.5 — SynapseError subclasses render as 中文 原因+建议, no traceback."""
    from synapse.adapters.cli import _friendly_error
    from synapse.core.exceptions import (
        ProviderError, ConfigError, ToolError, SandboxError, PlannerError,
    )

    for exc in (
        ProviderError("401 auth"),
        ConfigError("bad yaml"),
        ToolError("boom"),
        SandboxError("blocked"),
        PlannerError("loop"),
    ):
        out = _friendly_error(exc)
        assert "原因：" in out and "建议：" in out
        assert "Traceback" not in out

    # A plain (non-Synapse) error still hides the traceback and gives a hint.
    assert "原因：" in _friendly_error(RuntimeError("kaboom"))
    assert "建议：" in _friendly_error(RuntimeError("kaboom"))


def test_main_no_subcommand_does_not_crash_on_optional_args(monkeypatch):
    """`synapse` with no subcommand must not AttributeError on provider/model/mode,
    which only exist on the run/chat subparsers (regression for the top-level
    Namespace missing those attributes)."""
    from synapse.adapters import cli

    captured = {}

    async def fake_main(config_path=None, resume=None, provider=None, model=None, mode=None):
        captured["provider"] = provider
        captured["model"] = model
        captured["mode"] = mode

    monkeypatch.setattr(cli, "_main_interface", fake_main)
    monkeypatch.setattr("sys.argv", ["synapse"])

    cli.main()

    assert captured == {"provider": None, "model": None, "mode": None}


def test_cancel_handler_updates_display_and_planner():
    import signal
    from synapse.adapters.cli import _install_cancel_handler, _restore_cancel_handler

    class _Planner:
        def __init__(self):
            self.cancelled = False

        def request_cancel(self):
            self.cancelled = True

    planner = _Planner()

    class _Container:
        def resolve(self, _type):
            return planner

    class _Synapse:
        _container = _Container()

    class _Display:
        def __init__(self):
            self.label = ""

        def set_label(self, value):
            self.label = value

    display = _Display()
    holder = [display]
    previous = _install_cancel_handler(_Synapse(), holder)
    try:
        handler = signal.getsignal(signal.SIGINT)
        handler(signal.SIGINT, None)
    finally:
        _restore_cancel_handler(previous)

    assert planner.cancelled is True
    assert "取消" in display.label


def test_ollama_model_is_ready_without_api_key():
    from synapse.adapters.cli import _available_models
    from synapse.config.schema import SynapseConfig

    config = SynapseConfig()
    config.provider.provider = "ollama"
    config.provider.model = "qwen3.5:4b"
    available, _ = _available_models(config)
    assert any(e.provider == "ollama" and e.model == "qwen3.5:4b" for e in available)


def test_first_run_wizard_plain_writes_models_json(tmp_path, monkeypatch):
    from synapse.adapters import cli
    from synapse.config.models import load_models_config
    from synapse.config.schema import SynapseConfig

    models_file = tmp_path / "models.json"
    answers = iter(["deepseek", "deepseek-v4-pro"])
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _prompt="": "sk-test")
    monkeypatch.setattr("synapse.config.models.models_config_path", lambda: models_file)
    monkeypatch.setattr(cli, "models_config_path", lambda: models_file)

    cli._first_run_wizard_plain(SynapseConfig())

    registry = load_models_config(models_file)
    assert registry is not None
    assert registry.default_provider == "deepseek"
    assert registry.default_model == "deepseek-v4-pro"
    assert registry.providers["deepseek"].api_key == "sk-test"


def test_repl_enter_submits_content():
    """The REPL prompt must submit on Enter. key_processor picks the LAST
    matching binding (matches[-1]), so the custom Enter binding is merged AFTER
    load_key_bindings — with the order reversed, the multiline `_newline`
    binding won and Enter just inserted a newline (the reported bug). The
    handler must not call event.stop() — KeyPressEvent has no such method and
    it crashed the app with an unhandled exception."""
    import threading
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
    from prompt_toolkit.key_binding.defaults import load_key_bindings
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.shortcuts import PromptSession

    kb = KeyBindings()

    @kb.add("enter")
    def _(event):
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _(event):
        event.current_buffer.insert_text("\n")

    with create_pipe_input() as inp:
        inp.send_text("hello world\r")
        s = PromptSession(
            multiline=True, history=InMemoryHistory(),
            input=inp, output=DummyOutput(),
            key_bindings=merge_key_bindings([load_key_bindings(), kb]),
            complete_while_typing=True,
        )
        result = {}

        def _run():
            try:
                result["v"] = s.prompt("> ")
            except Exception as e:
                result["e"] = repr(e)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=5)
        assert "v" in result, f"prompt did not submit: {result.get('e')}"
        assert result["v"] == "hello world"
