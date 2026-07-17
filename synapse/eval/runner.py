"""Benchmark Runner — executes benchmark tasks against Synapse Agent.

Provides the data model (BenchmarkTask, TaskResult, BenchmarkResult, Benchmark)
and BenchmarkRunner that orchestrates execution and metric aggregation.

This module lives in ``eval/`` and only consumes ``core/`` and ``protocols/``.
It never imports from ``modules/`` directly.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Awaitable
from dataclasses import dataclass, field

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
        run_task: Callable[[str], Awaitable[AgentResult]],
    ) -> BenchmarkResult:
        """Execute every task in *benchmark* and aggregate results.

        Parameters
        ----------
        benchmark:
            The benchmark to execute.
        run_task:
            An async callable ``(task: str) -> AgentResult`` that executes a
            single task description.  It is called once per task, in order.

        Returns
        -------
        BenchmarkResult
            Aggregated statistics and per-task results.
        """
        t0 = time.monotonic()
        results: list[TaskResult] = []
        completed = 0
        failed = 0

        for task in benchmark.tasks:
            t_start = time.monotonic()
            try:
                agent_result = await run_task(task.description)
            except Exception as exc:
                results.append(TaskResult(
                    task_id=task.id,
                    status="error",
                    output="",
                    duration_ms=int((time.monotonic() - t_start) * 1000),
                    error=str(exc),
                ))
                failed += 1
                continue

            status = agent_result.status.value
            duration_ms = int((time.monotonic() - t_start) * 1000)

            if status == ResultStatus.SUCCESS:
                completed += 1
            elif status in (ResultStatus.FAILED,):
                failed += 1
            # PARTIAL counts as completed (partial success)

            results.append(TaskResult(
                task_id=task.id,
                status=status,
                output=agent_result.output,
                duration_ms=duration_ms,
            ))

        total_ms = int((time.monotonic() - t0) * 1000)

        return BenchmarkResult(
            name=benchmark.name,
            total=len(benchmark.tasks),
            completed=completed,
            failed=failed,
            results=results,
            duration_ms=total_ms,
        )
