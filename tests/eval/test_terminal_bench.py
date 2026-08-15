"""Tests for Terminal-Bench-compatible adapters."""

import sys
from pathlib import Path

import pytest

from synapse.eval.benchmarks.terminal import TerminalBenchAdapter, TerminalSmokeBenchmark
from synapse.eval.runner import BenchmarkTask
from synapse.protocols.planner import AgentResult, ResultStatus


def test_terminal_smoke_grader_checks_exact_file(tmp_path: Path):
    task = TerminalSmokeBenchmark.tasks()[0]
    (tmp_path / "marker.txt").write_text("SYNAPSE_TERMINAL_OK\n", encoding="utf-8")
    facts = TerminalBenchAdapter.grade_workspace(task, tmp_path)
    assert facts["passed"] is True


def test_terminal_preflight_rejects_a_baseline_that_already_passes(tmp_path: Path):
    task = TerminalSmokeBenchmark.tasks()[0]
    (tmp_path / "marker.txt").write_text("SYNAPSE_TERMINAL_OK\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="baseline already passes"):
        TerminalBenchAdapter.preflight(task, tmp_path)


def test_terminal_preflight_requires_an_external_grader(tmp_path: Path):
    task = BenchmarkTask(id="ungrounded", description="do work")

    with pytest.raises(RuntimeError, match="no external grader"):
        TerminalBenchAdapter.preflight(task, tmp_path)


def test_terminal_grade_uses_external_facts_not_runtime_status():
    task = TerminalSmokeBenchmark.tasks()[0]
    failed = AgentResult(status=ResultStatus.PARTIAL, output="done")
    grade = TerminalBenchAdapter.grade(task, failed, {"terminal": {"passed": True}})
    assert grade.passed is True
    assert grade.score == 1.0
    assert grade.details["agent_status"] == "partial"


def test_terminal_jsonl_loader(tmp_path: Path):
    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text(
        '{"task_id":"t1","instruction":"run a command","category":"shell"}\n',
        encoding="utf-8",
    )
    tasks = TerminalBenchAdapter.tasks(dataset)
    assert tasks[0].id == "t1"
    assert tasks[0].metadata["harness"] == "terminal-bench"


def test_terminal_command_grader_requires_explicit_host_trust(tmp_path: Path):
    marker = tmp_path / "must-not-run"
    task = BenchmarkTask(
        id="command",
        description="command",
        metadata={
            "grader_command": [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')",
            ],
        },
    )

    with pytest.raises(RuntimeError, match="trusted_host_execution=True"):
        TerminalBenchAdapter.grade_workspace(task, tmp_path)
    assert not marker.exists()


def test_terminal_command_grader_runs_only_after_explicit_trust(tmp_path: Path):
    task = BenchmarkTask(
        id="command",
        description="command",
        metadata={
            "grader_command": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('graded.txt').write_text('ok')",
            ],
        },
    )

    facts = TerminalBenchAdapter.grade_workspace(
        task,
        tmp_path,
        trusted_host_execution=True,
    )

    assert facts["passed"] is True
    assert (tmp_path / "graded.txt").read_text(encoding="utf-8") == "ok"


def test_terminal_string_command_reports_shlex_errors(tmp_path: Path):
    task = BenchmarkTask(
        id="invalid",
        description="invalid",
        metadata={"grader_command": '"unterminated'},
    )

    facts = TerminalBenchAdapter.grade_workspace(
        task,
        tmp_path,
        trusted_host_execution=True,
    )

    assert facts["passed"] is False
    assert facts["output"] == "invalid grader command"
    assert facts["grader_error"] == "invalid grader command"


def test_terminal_grader_config_error_is_not_a_verified_failure():
    task = TerminalSmokeBenchmark.tasks()[0]
    agent = AgentResult(status=ResultStatus.SUCCESS, output="done")

    with pytest.raises(RuntimeError, match="invalid grader command"):
        TerminalBenchAdapter.grade(
            task,
            agent,
            {"terminal": {
                "passed": False,
                "grader_error": "invalid grader command",
            }},
        )
