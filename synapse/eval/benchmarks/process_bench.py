"""Process Quality Benchmark — self-built benchmark for process-quality eval.

The ``ProcessQualityBenchmark`` measures **how** an agent solves problems,
not just whether the final patch passes tests.  It targets Synapse's core
differentiation: process quality.

Four dimensions are assessed:

1. **Reuse detection** — does the agent find and adopt existing patterns
   before writing new code?
2. **Root-cause identification** — does the agent trace symptoms back to
   their origin, or does it apply superficial patches?
3. **Test persistence** — does the agent preserve and extend existing
   tests, or does it delete / comment out failing tests?
4. **Instruction following** — does the agent adhere to project conventions
   and task-specific constraints?

Each task has ``expected_process_scores`` that represent the minimum
acceptable quality thresholds on each relevant dimension.
"""

from __future__ import annotations

from synapse.eval.runner import Benchmark, BenchmarkTask, TaskGrade
from synapse.protocols.planner import AgentResult, ResultStatus


# ---------------------------------------------------------------------------
# ProcessQualityBenchmark
# ---------------------------------------------------------------------------


class ProcessQualityBenchmark:
    """Self-built benchmark focusing on process-quality evaluation.

    Usage::

        from synapse.eval.benchmarks.process_bench import ProcessQualityBenchmark
        from synapse.eval.runner import Benchmark

        benchmark = Benchmark(
            name="process-quality",
            tasks=ProcessQualityBenchmark.tasks(),
        )
    """

    BENCHMARK_NAME: str = "process_quality"

    @classmethod
    def benchmark(cls, tasks: list[BenchmarkTask] | None = None) -> Benchmark:
        """Build a scored benchmark for the generic runner."""
        return Benchmark(
            name=cls.BENCHMARK_NAME,
            tasks=tasks if tasks is not None else cls.tasks(),
            grader=cls.grade,
            metadata={
                "grader": "runtime_process_snapshot",
                "functional": False,
                "dataset_manifest": {
                    "version": "1",
                    "source": "bundled process-quality task set",
                    "license": "not_declared",
                },
            },
        )

    @staticmethod
    def grade(task, agent_result: AgentResult, run_score: dict | None) -> TaskGrade:
        """Compare runtime process metrics with task-specific thresholds."""
        actual = (run_score or {}).get("process", {})
        checks: dict[str, dict[str, float | bool]] = {}
        ratios: list[float] = []
        for metric, expected in task.expected_process_scores.items():
            observed = float(actual.get(metric, 0.0) or 0.0)
            if metric.startswith("instruction_drift"):
                passed = observed <= expected
                ratio = 1.0 if expected == 0 and passed else (
                    min(1.0, expected / max(observed, 1.0)) if expected else 0.0
                )
            else:
                passed = observed >= expected
                ratio = min(1.0, observed / expected) if expected > 0 else 1.0
            checks[metric] = {"observed": observed, "expected": expected, "passed": passed}
            ratios.append(ratio)

        score = sum(ratios) / len(ratios) if ratios else 0.0
        passed = bool(agent_result.status == ResultStatus.SUCCESS and checks and all(
            bool(item["passed"]) for item in checks.values()
        ))
        return TaskGrade(
            passed=passed,
            score=score,
            reason="process thresholds met" if passed else "process thresholds not met",
            details={"metrics": checks, "agent_status": agent_result.status.value},
        )

    # ------------------------------------------------------------------
    # Task definitions
    # ------------------------------------------------------------------

    @classmethod
    def tasks(cls) -> list[BenchmarkTask]:
        """Return the predefined set of process-quality benchmark tasks.

        Returns
        -------
        list[BenchmarkTask]
            Between 4 and 8 tasks covering all four quality dimensions.
        """
        return [
            # ==========================================================
            # DIMENSION 1: Reuse Detection
            # ==========================================================

            BenchmarkTask(
                id="reuse-1",
                description=(
                    "Add a utility function that generates a random hex string "
                    "of configurable length. Before writing new code, search the "
                    "codebase for existing implementations or similar utilities "
                    "that could be reused or adapted."
                ),
                metadata={"category": "reuse-detection", "difficulty": "medium"},
                expected_process_scores={
                    "reuse_attempted": 1.0,
                    "reuse_found": 0.8,
                },
            ),

            BenchmarkTask(
                id="reuse-2",
                description=(
                    "We need a logging helper that formats timestamps in ISO 8601. "
                    "The project already has several logging-related modules — "
                    "find and extend the existing logger rather than creating a "
                    "new one from scratch."
                ),
                metadata={"category": "reuse-detection", "difficulty": "easy"},
                expected_process_scores={
                    "reuse_attempted": 1.0,
                    "reuse_adopted": 1.0,
                },
            ),

            # ==========================================================
            # DIMENSION 2: Root-Cause Identification
            # ==========================================================

            BenchmarkTask(
                id="rootcause-1",
                description=(
                    "After the latest deployment, the login page returns a 500 "
                    "error instead of redirecting to the dashboard. The error "
                    "log shows a KeyError in session handling. Find the root "
                    "cause — do NOT just wrap the KeyError in a try/except. "
                    "Trace back to understand why the session key is missing."
                ),
                metadata={"category": "root-cause", "difficulty": "hard"},
                expected_process_scores={
                    "root_cause_accuracy": 0.8,
                    "instruction_drift_at_round": 0.0,
                },
            ),

            BenchmarkTask(
                id="rootcause-2",
                description=(
                    "Users report that profile pictures intermittently fail to "
                    "load after the CDN migration. The on-call runbook suggests "
                    "clearing the CDN cache, but this only helps temporarily. "
                    "Identify the underlying issue and implement a durable fix."
                ),
                metadata={"category": "root-cause", "difficulty": "hard"},
                expected_process_scores={
                    "root_cause_accuracy": 0.9,
                },
            ),

            # ==========================================================
            # DIMENSION 3: Test Persistence
            # ==========================================================

            BenchmarkTask(
                id="testpersist-1",
                description=(
                    "Refactor the UserService class to use the new database "
                    "abstraction layer. Make sure ALL existing tests continue "
                    "to pass — do not delete, skip, or comment out any test. "
                    "If a test fails because of the refactor, update the test "
                    "to match the new behaviour."
                ),
                metadata={"category": "test-persistence", "difficulty": "medium"},
                expected_process_scores={
                    "tests_persisted": 1.0,
                    "test_persistence_rate": 1.0,
                },
            ),

            BenchmarkTask(
                id="testpersist-2",
                description=(
                    "Add input validation to the API endpoint handler. Write "
                    "new tests for edge cases (empty input, oversized input, "
                    "malformed JSON). Ensure all existing tests for this "
                    "endpoint still pass — run the full test suite before "
                    "considering the task done."
                ),
                metadata={"category": "test-persistence", "difficulty": "medium"},
                expected_process_scores={
                    "tests_persisted": 1.0,
                    "test_persistence_rate": 1.0,
                },
            ),

            # ==========================================================
            # DIMENSION 4: Instruction Following
            # ==========================================================

            BenchmarkTask(
                id="instruct-1",
                description=(
                    "Follow the project's coding standards defined in "
                    "CONTRIBUTING.md when making changes: use type hints on all "
                    "new functions, write docstrings in Google style, and keep "
                    "functions under 50 lines. Add pagination support to the "
                    "list_users endpoint following these conventions exactly."
                ),
                metadata={"category": "instruction-following", "difficulty": "medium"},
                expected_process_scores={
                    "instruction_drift_at_round": 0.0,
                    "plan_quality_score": 0.8,
                },
            ),

            BenchmarkTask(
                id="instruct-2",
                description=(
                    "The task requires a performance optimization for the search "
                    "endpoint. IMPORTANT: before making any changes, benchmark "
                    "the current performance. After implementing the optimization, "
                    "benchmark again and report the improvement. Do NOT skip the "
                    "benchmarking steps even if the optimization seems obvious."
                ),
                metadata={"category": "instruction-following", "difficulty": "hard"},
                expected_process_scores={
                    "instruction_drift_at_round": 0.0,
                    "plan_quality_score": 0.9,
                },
            ),
        ]
