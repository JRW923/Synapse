"""Tests for BenchmarkRunner — executes benchmark tasks against Synapse Agent."""

import pytest

from synapse.eval.runner import (
    BenchmarkRunner,
    Benchmark,
    BenchmarkTask,
    TaskGrade,
    TaskResult,
    BenchmarkResult,
)
from synapse.protocols.planner import AgentResult, ResultStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _MockRunTask:
    """Fake run_task that returns configurable AgentResults."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._results: dict[str, AgentResult] = {}

    def set_result(self, task_id: str, result: AgentResult) -> None:
        self._results[task_id] = result

    async def __call__(self, task: str) -> AgentResult:
        self.calls.append(task)
        return self._results.get(task, AgentResult(
            status=ResultStatus.SUCCESS,
            output=f"done: {task}",
        ))


@pytest.fixture
def mock_run_task() -> _MockRunTask:
    return _MockRunTask()


@pytest.fixture
def simple_benchmark() -> Benchmark:
    return Benchmark(
        name="simple-test",
        tasks=[
            BenchmarkTask(id="t1", description="task one"),
            BenchmarkTask(id="t2", description="task two"),
            BenchmarkTask(id="t3", description="task three"),
        ],
    )


# ---------------------------------------------------------------------------
# Test: runner executes simple benchmark
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_runner_executes_simple_benchmark(
    simple_benchmark: Benchmark,
    mock_run_task: _MockRunTask,
) -> None:
    """Runner should execute every task in the benchmark and aggregate results."""
    runner = BenchmarkRunner()
    result = await runner.run(simple_benchmark, mock_run_task)

    # Basic counts
    assert isinstance(result, BenchmarkResult)
    assert result.name == "simple-test"
    assert result.total == 3
    assert result.completed == 3
    assert result.failed == 0

    # Every task was invoked
    assert len(mock_run_task.calls) == 3
    assert "task one" in mock_run_task.calls
    assert "task two" in mock_run_task.calls
    assert "task three" in mock_run_task.calls

    # Every task has a result entry
    task_ids = [r.task_id for r in result.results]
    assert "t1" in task_ids
    assert "t2" in task_ids
    assert "t3" in task_ids

    # Duration is recorded
    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_runner_grades_tasks_and_writes_bounded_report(tmp_path) -> None:
    async def run_task(task: str):
        return AgentResult(
            status=ResultStatus.SUCCESS if task == "pass" else ResultStatus.PARTIAL,
            output="x" * 5000,
        ), {"efficiency": {"tool_call_count": 1}}

    def grade(task, result, run_score):
        return TaskGrade(
            passed=result.status == ResultStatus.SUCCESS,
            score=1.0 if result.status == ResultStatus.SUCCESS else 0.25,
            reason=task.id,
            details={"has_score": run_score is not None},
        )

    benchmark = Benchmark(
        name="graded",
        tasks=[
            BenchmarkTask(id="p", description="pass", metadata={"category": "functional"}),
            BenchmarkTask(id="q", description="partial", metadata={"category": "functional"}),
        ],
        grader=grade,
    )
    report = tmp_path / "report.json"
    result = await BenchmarkRunner().run(benchmark, run_task, report_path=report)

    assert result.passed == 1
    assert result.pass_rate == 0.5
    assert result.mean_score == 0.625
    assert result.by_category["functional"]["passed"] == 1
    payload = report.read_text(encoding="utf-8")
    assert len(payload) < 10_000
    assert '"run_score"' in payload
