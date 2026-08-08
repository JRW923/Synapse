"""Tests for Terminal-Bench-compatible adapters."""

from pathlib import Path

from synapse.eval.benchmarks.terminal import TerminalBenchAdapter, TerminalSmokeBenchmark
from synapse.protocols.planner import AgentResult, ResultStatus


def test_terminal_smoke_grader_checks_exact_file(tmp_path: Path):
    task = TerminalSmokeBenchmark.tasks()[0]
    (tmp_path / "marker.txt").write_text("SYNAPSE_TERMINAL_OK\n", encoding="utf-8")
    facts = TerminalBenchAdapter.grade_workspace(task, tmp_path)
    assert facts["passed"] is True


def test_terminal_grade_requires_runtime_success():
    task = TerminalSmokeBenchmark.tasks()[0]
    failed = AgentResult(status=ResultStatus.PARTIAL, output="done")
    grade = TerminalBenchAdapter.grade(task, failed, {"terminal": {"passed": True}})
    assert grade.passed is False
    assert grade.score == 0.5


def test_terminal_jsonl_loader(tmp_path: Path):
    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text(
        '{"task_id":"t1","instruction":"run a command","category":"shell"}\n',
        encoding="utf-8",
    )
    tasks = TerminalBenchAdapter.tasks(dataset)
    assert tasks[0].id == "t1"
    assert tasks[0].metadata["harness"] == "terminal-bench"
