"""Benchmark Runner — executes benchmark tasks against Synapse Agent.

Provides the data model (BenchmarkTask, TaskResult, BenchmarkResult, Benchmark)
and BenchmarkRunner that orchestrates execution and metric aggregation.

This module lives in ``eval/`` and only consumes ``core/`` and ``protocols/``.
It never imports from ``modules/`` directly.
"""

from __future__ import annotations

import time
import inspect
import json
from collections.abc import Callable, Awaitable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from synapse.protocols.planner import AgentResult, ResultStatus


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class BenchmarkTask:
    """A single benchmark task.

    Attributes
    ----------
    id:
        Unique task identifier within the benchmark.
    description:
        Natural-language task description passed to the agent.
    metadata:
        Free-form metadata (e.g. ``repo_url``, ``base_commit``, ``date`` for
        SWE-bench tasks; ``category`` for process-quality tasks).
    expected_process_scores:
        Expected process-quality scores keyed by metric name.  Used by
        ``ProcessQualityBenchmark`` to validate that the agent's process
        behaviour meets quality targets.
    """

    id: str
    description: str
    metadata: dict = field(default_factory=dict)
    expected_process_scores: dict[str, float] = field(default_factory=dict)


@dataclass
class TaskGrade:
    """Deterministic grade returned by a benchmark-specific evaluator.

    ``score`` is always normalized to ``0..1`` so functional and process
    benchmarks can be aggregated without pretending their raw metrics are
    directly comparable.
    """

    passed: bool
    score: float
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.score = max(0.0, min(1.0, float(self.score)))


@dataclass
class TaskResult:
    """Result of executing a single benchmark task.

    Attributes
    ----------
    task_id:
        The ``BenchmarkTask.id`` this result corresponds to.
    status:
        One of ``"success"``, ``"partial"``, ``"failed"``, ``"error"``.
    output:
        Final output text from the agent.
    duration_ms:
        Wall-clock time for this task in milliseconds.
    error:
        Error message if the task raised an exception, ``None`` otherwise.
    """

    task_id: str
    status: str
    output: str = ""
    duration_ms: int = 0
    error: str | None = None
    passed: bool = False
    score: float = 0.0
    category: str = "uncategorized"
    grade_reason: str = ""
    grade_details: dict[str, Any] = field(default_factory=dict)
    run_score: dict[str, Any] | None = None


@dataclass
class BenchmarkResult:
    """Aggregated result of running an entire benchmark.

    Attributes
    ----------
    name:
        Human-readable benchmark name.
    total:
        Total number of tasks in the benchmark.
    completed:
        Number of tasks whose status is ``"success"``.
    failed:
        Number of tasks whose status is ``"failed"`` or ``"error"``.
    results:
        Per-task results in execution order.
    duration_ms:
        Total wall-clock time across all tasks.
    """

    name: str
    total: int
    completed: int = 0
    failed: int = 0
    results: list[TaskResult] = field(default_factory=list)
    duration_ms: int = 0
    passed: int = 0
    pass_rate: float = 0.0
    mean_score: float = 0.0
    by_category: dict[str, dict[str, float | int]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe report with bounded task output."""
        tasks = []
        for result in self.results:
            item = {
                "task_id": result.task_id,
                "status": result.status,
                "output": result.output[-4000:],
                "duration_ms": result.duration_ms,
                "error": result.error,
                "passed": result.passed,
                "score": result.score,
                "category": result.category,
                "grade_reason": result.grade_reason,
                "grade_details": result.grade_details,
                "run_score": result.run_score,
            }
            tasks.append(item)
        return {
            "name": self.name,
            "total": self.total,
            "completed": self.completed,
            "failed": self.failed,
            "passed": self.passed,
            "pass_rate": self.pass_rate,
            "mean_score": self.mean_score,
            "duration_ms": self.duration_ms,
            "started_at": self.started_at,
            "metadata": self.metadata,
            "by_category": self.by_category,
            "results": tasks,
        }

    def write_json(self, path: str | Path) -> Path:
        """Persist the report and return its resolved path."""
        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return target


@dataclass
class Benchmark:
    """A collection of benchmark tasks.

    This is the common type consumed by ``BenchmarkRunner``.  Concrete
    benchmarks (SWE-bench, process-quality, custom) produce a ``Benchmark``
    instance whose tasks are executed sequentially.

    Attributes
    ----------
    name:
        Human-readable benchmark name.
    tasks:
        Ordered list of ``BenchmarkTask`` instances.
    """

    name: str
    tasks: list[BenchmarkTask] = field(default_factory=list)
    grader: Callable[
        [BenchmarkTask, AgentResult, dict[str, Any] | None], TaskGrade
    ] | None = field(default=None, repr=False, compare=False)
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# BenchmarkRunner
# ---------------------------------------------------------------------------


class BenchmarkRunner:
    """Executes a benchmark against a task-running function.

    The runner is responsible for:
    1. Iterating over every task in the benchmark.
    2. Calling *run_task* for each task's description.
    3. Collecting per-task ``TaskResult`` instances.
    4. Aggregating results into a ``BenchmarkResult``.

    The runner does **not** create the agent itself.  Instead it accepts a
    ``run_task`` async callable (e.g. ``Synapse.run`` or a test double) so
    that the caller controls agent creation and lifecycle.  This keeps
    ``eval/`` decoupled from ``modules/``.

    Usage::

        from synapse import Synapse
        from synapse.eval.runner import BenchmarkRunner, Benchmark, BenchmarkTask

        synapse = Synapse(provider="anthropic")
        benchmark = Benchmark(name="my-bench", tasks=[
            BenchmarkTask(id="1", description="Fix bug in auth.py"),
            BenchmarkTask(id="2", description="Add logging to parser.py"),
        ])
        runner = BenchmarkRunner()
        result = await runner.run(benchmark, synapse.run)
    """

    async def run(
        self,
        benchmark: Benchmark,
        run_task: Callable[[str], Awaitable[Any]],
        grade_task: Callable[
            [BenchmarkTask, AgentResult, dict[str, Any] | None], TaskGrade
        ] | None = None,
        report_path: str | Path | None = None,
        metadata: dict[str, Any] | None = None,
        task_runner: Callable[[BenchmarkTask], Awaitable[Any]] | None = None,
    ) -> BenchmarkResult:
        """Execute every task in *benchmark* and aggregate results.

        Parameters
        ----------
        benchmark:
            The benchmark to execute.
        run_task:
            An async callable ``(task: str) -> AgentResult`` that executes a
            single task description. It may also return ``(AgentResult,
            run_score_dict)`` so runtime metrics can be attached to reports.
        task_runner:
            Optional task-aware callback. When supplied it receives the full
            ``BenchmarkTask`` and is useful for isolated checkout/container
            benchmarks that need task metadata.
        grade_task:
            Optional deterministic grader. If omitted, the benchmark's grader
            is used, then a successful AgentResult is treated as score ``1``.
        report_path:
            Optional JSON path for a bounded, machine-readable report.

        Returns
        -------
        BenchmarkResult
            Aggregated statistics and per-task results.
        """
        t0 = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat()
        results: list[TaskResult] = []
        completed = 0
        failed = 0
        passed = 0

        for task in benchmark.tasks:
            t_start = time.monotonic()
            try:
                execution = await (
                    task_runner(task) if task_runner is not None else run_task(task.description)
                )
                run_score = None
                if (
                    isinstance(execution, tuple)
                    and len(execution) == 2
                    and isinstance(execution[0], AgentResult)
                ):
                    agent_result, run_score = execution
                else:
                    agent_result = execution
                if not isinstance(agent_result, AgentResult):
                    raise TypeError(
                        "run_task must return AgentResult or (AgentResult, run_score)"
                    )
            except Exception as exc:
                results.append(TaskResult(
                    task_id=task.id,
                    status="error",
                    output="",
                    duration_ms=int((time.monotonic() - t_start) * 1000),
                    error=str(exc),
                    category=str(task.metadata.get("category", "uncategorized")),
                ))
                failed += 1
                continue

            status = (
                agent_result.status.value
                if hasattr(agent_result.status, "value")
                else str(agent_result.status)
            )
            duration_ms = int((time.monotonic() - t_start) * 1000)

            if status == ResultStatus.SUCCESS.value:
                completed += 1
            elif status in (ResultStatus.FAILED.value, "error"):
                failed += 1
            # PARTIAL counts as completed (partial success)

            grader = grade_task or benchmark.grader
            try:
                if grader is None:
                    grade = TaskGrade(
                        passed=status == ResultStatus.SUCCESS.value,
                        score=1.0 if status == ResultStatus.SUCCESS.value else 0.0,
                        reason="agent reported success" if status == ResultStatus.SUCCESS.value
                        else f"agent reported {status}",
                    )
                else:
                    grade = grader(task, agent_result, run_score)
                    if inspect.isawaitable(grade):
                        grade = await grade
                    if not isinstance(grade, TaskGrade):
                        raise TypeError("grader must return TaskGrade")
            except Exception as exc:
                grade = TaskGrade(False, 0.0, reason=f"grader error: {exc}")

            if grade.passed:
                passed += 1

            results.append(TaskResult(
                task_id=task.id,
                status=status,
                output=agent_result.output,
                duration_ms=duration_ms,
                passed=grade.passed,
                score=grade.score,
                category=str(task.metadata.get("category", "uncategorized")),
                grade_reason=grade.reason,
                grade_details=grade.details,
                run_score=run_score,
            ))

        total_ms = int((time.monotonic() - t0) * 1000)
        total = len(benchmark.tasks)
        category_buckets: dict[str, dict[str, float | int]] = {}
        for result in results:
            bucket = category_buckets.setdefault(
                result.category,
                {"total": 0, "passed": 0, "pass_rate": 0.0, "mean_score": 0.0},
            )
            bucket["total"] = int(bucket["total"]) + 1
            bucket["passed"] = int(bucket["passed"]) + int(result.passed)
            bucket["mean_score"] = float(bucket["mean_score"]) + result.score
        for bucket in category_buckets.values():
            count = int(bucket["total"])
            bucket["pass_rate"] = round(int(bucket["passed"]) / count, 4) if count else 0.0
            bucket["mean_score"] = round(float(bucket["mean_score"]) / count, 4) if count else 0.0

        result = BenchmarkResult(
            name=benchmark.name,
            total=total,
            completed=completed,
            failed=failed,
            results=results,
            duration_ms=total_ms,
            passed=passed,
            pass_rate=round(passed / total, 4) if total else 0.0,
            mean_score=round(sum(item.score for item in results) / total, 4) if total else 0.0,
            by_category=category_buckets,
            metadata={**benchmark.metadata, **(metadata or {})},
            started_at=started_at,
        )
        if report_path is not None:
            result.write_json(report_path)
        return result
