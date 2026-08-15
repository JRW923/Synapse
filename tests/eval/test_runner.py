"""Tests for BenchmarkRunner — executes benchmark tasks against Synapse Agent."""

import json

import pytest

from synapse.eval.runner import (
    BenchmarkRunner,
    Benchmark,
    BenchmarkTask,
    TaskGrade,
    TaskResult,
    BenchmarkResult,
    _runner_comparability_envelope,
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


class _HelperBackedGrader:
    @staticmethod
    def execute() -> bool:
        return True

    @staticmethod
    def grade(_task, _result, _score) -> TaskGrade:
        passed = _HelperBackedGrader.execute()
        return TaskGrade(passed, float(passed))


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


def test_runner_comparability_envelope_captures_fixed_budget_and_permissions():
    envelope = _runner_comparability_envelope({
        "provider": {
            "max_tokens": 4096,
            "timeout_seconds": 120,
            "max_retries": 3,
        },
        "planning": {
            "max_iterations": 50,
            "max_tokens_per_task": 200_000,
            "total_timeout_seconds": 300,
            "max_tool_result_chars": 16_000,
        },
        "context": {"total_tokens": 32_000},
        "tools": {"enabled": ["read", "shell"], "allowlist_commands": ["git"]},
        "security": {
            "sandbox_enabled": True,
            "sandbox_mode": "enforce",
            "sandbox_backend": "docker",
            "sandbox_network": False,
            "sandbox_docker_image": "fixture@sha256:abc",
            "auth_confirmation": True,
            "allowed_paths": [],
            "allow_external": False,
        },
        "runtime": {"enable_external_tools": False, "mcp_servers": []},
    }, {"model_id": "fixture-model"})["comparability"]

    assert envelope["model_id"] == "fixture-model"
    assert envelope["budgets"]["provider_max_retries"] == 3
    assert envelope["budgets"]["max_tool_result_chars"] == 16_000
    assert envelope["permissions"]["sandbox_docker_image"] == "fixture@sha256:abc"


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


@pytest.mark.asyncio
async def test_runner_report_redacts_free_form_output_and_errors(tmp_path) -> None:
    secret = "private-grader-output-sk-test"

    async def run_task(_task: str):
        return AgentResult(status=ResultStatus.SUCCESS, output=secret), {
            "terminal": {"output": secret},
            "process_hint": secret,
        }

    def grade(_task, _result, _run_score):
        return TaskGrade(
            True, 1.0, reason=secret,
            details={"grader_output": secret},
        )

    report = tmp_path / "redacted.json"
    await BenchmarkRunner().run(
        Benchmark(
            name="redacted",
            tasks=[BenchmarkTask(id="one", description="one")],
            grader=grade,
        ),
        run_task,
        report_path=report,
    )

    payload = report.read_text(encoding="utf-8")
    assert secret not in payload
    assert "sha256=" in payload


@pytest.mark.asyncio
async def test_runner_report_sanitizes_free_form_metadata_recursively(tmp_path) -> None:
    secret = "metadata-secret-sk-test"
    report = tmp_path / "metadata.json"

    async def run_task(_task: str):
        return AgentResult(status=ResultStatus.SUCCESS, output="ok")

    result = await BenchmarkRunner().run(
        Benchmark(
            name="metadata",
            tasks=[BenchmarkTask(id="one", description="one")],
            metadata={"provider": "fixture"},
        ),
        run_task,
        metadata={
            "api_key": secret,
            "nested": {
                "authorization": secret,
                "openaiApiKey": secret,
                "notes": secret,
            },
        },
        report_path=report,
    )

    payload = result.to_dict()
    assert payload["metadata"]["provider"] == "fixture"
    assert payload["metadata"]["api_key"] == "<redacted>"
    assert payload["metadata"]["nested"]["authorization"] == "<redacted>"
    assert payload["metadata"]["nested"]["openaiApiKey"] == "<redacted>"
    assert "sha256=" in payload["metadata"]["nested"]["notes"]
    assert secret not in report.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_runner_task_runner_receives_metadata():
    benchmark = Benchmark(name="task-aware", tasks=[
        BenchmarkTask(id="x", description="ignored", metadata={"workspace": "isolated"}),
    ])
    seen = []

    async def task_runner(task):
        seen.append(task.metadata["workspace"])
        return AgentResult(status=ResultStatus.SUCCESS, output="ok")

    result = await BenchmarkRunner().run(
        benchmark,
        lambda _description: AgentResult(status=ResultStatus.FAILED, output="wrong callback"),
        task_runner=task_runner,
    )
    assert seen == ["isolated"]
    assert result.passed == 0
    assert result.scored_attempt_total == 0
    assert result.agent_reported_successes == 1


@pytest.mark.asyncio
async def test_runner_repeat_reports_ci_pass_at_k_and_runtime_totals():
    benchmark = Benchmark(
        name="repeat",
        tasks=[BenchmarkTask(id="t", description="task")],
        grader=lambda _task, result, _score: TaskGrade(
            result.status == ResultStatus.SUCCESS,
            float(result.status == ResultStatus.SUCCESS),
        ),
    )
    calls = 0

    async def run_task(_task):
        nonlocal calls
        calls += 1
        status = ResultStatus.SUCCESS if calls == 2 else ResultStatus.FAILED
        return AgentResult(status=status, output="ok"), {
            "model_id": "fixture-model",
            "run_id": f"run-{calls}",
            "efficiency": {
                "tokens_input": 10,
                "tokens_output": 5,
                "token_count_source": "exact",
                "tool_call_count": 2,
                "tool_success_count": 1,
                "cost_estimate_usd": 0.01,
                "cost_is_estimate": True,
                "input_cost_per_million_usd": 3.0,
                "output_cost_per_million_usd": 15.0,
            },
        }

    result = await BenchmarkRunner().run(benchmark, run_task, repeat=2)
    assert [item.task_id for item in result.results] == ["t#1", "t#2"]
    assert result.total == 2
    assert result.passed == 1
    assert result.pass_at_k == 1.0
    assert result.pass_at_k_ci95_by_k["1"] == [0.0, 1.0]
    assert result.pass_power_k_ci95_by_k["2"] == [0.0, 1.0]
    assert result.tokens_input == 20
    assert result.tokens_output == 10
    assert result.total_cost_usd == 0.02
    assert result.tool_success_rate == 0.5
    assert result.pass_rate_ci95[0] <= 0.5 <= result.pass_rate_ci95[1]
    assert result.tokens_per_passed_attempt == 30.0
    assert result.cost_per_passed_attempt_usd == 0.02
    assert result.efficiency_provenance == {
        "token_count_sources": ["exact"],
        "attempts_with_efficiency": 2,
        "attempts_with_token_counts": 2,
        "attempt_total": 2,
        "token_coverage": 1.0,
        "token_metrics_complete": True,
        "cost_is_estimate": True,
        "cost_rates_usd_per_million": [{"input": 3.0, "output": 15.0}],
    }
    assert result.p95_tokens == 15.0
    assert result.safety_violation_rate == 0.0
    assert result.infrastructure_failure_rate == 0.0
    assert result.reproducibility["actual_model_ids"] == ["fixture-model"]
    assert result.reproducibility["actual_run_ids"] == ["run-1", "run-2"]


@pytest.mark.asyncio
async def test_runner_separates_attempt_task_pass_at_k_and_pass_power_k():
    benchmark = Benchmark(
        name="matrix",
        tasks=[
            BenchmarkTask(id="task#one", description="one"),
            BenchmarkTask(id="task-two", description="two"),
        ],
        grader=lambda _task, result, _score: TaskGrade(
            result.status == ResultStatus.SUCCESS,
            float(result.status == ResultStatus.SUCCESS),
        ),
    )
    outcomes = {
        "task#one": iter([False, True, False]),
        "task-two": iter([False, False, False]),
    }

    async def task_runner(task):
        passed = next(outcomes[task.id])
        return AgentResult(
            status=ResultStatus.SUCCESS if passed else ResultStatus.FAILED,
            output="done",
        )

    result = await BenchmarkRunner().run(
        benchmark,
        lambda _description: None,
        task_runner=task_runner,
        repeat=3,
    )

    assert result.attempt_total == 6
    assert result.attempt_passed == 1
    assert result.attempt_pass_rate == 0.1667
    assert result.task_total == 2
    assert result.task_succeeded == 1
    assert result.task_success_rate == 0.5
    assert result.task_success_k == 3
    assert result.pass_at_k_by_k == {"1": 0.1667, "2": 0.3333, "3": 0.5}
    assert result.pass_power_k_by_k == {"1": 0.1667, "2": 0.0, "3": 0.0}
    assert result.results[0].task_id == "task#one#1"
    assert result.results[0].base_task_id == "task#one"
    assert [item.attempt for item in result.results[:3]] == [1, 2, 3]


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat", [0, -1, 1.5, True])
async def test_runner_rejects_invalid_repeat(repeat):
    benchmark = Benchmark(name="invalid", tasks=[])
    with pytest.raises(ValueError, match="positive integer"):
        await BenchmarkRunner().run(benchmark, lambda _description: None, repeat=repeat)


@pytest.mark.asyncio
async def test_runner_rejects_duplicate_task_ids():
    benchmark = Benchmark(name="duplicate", tasks=[
        BenchmarkTask(id="same", description="one"),
        BenchmarkTask(id="same", description="two"),
    ])
    with pytest.raises(ValueError, match="unique"):
        await BenchmarkRunner().run(benchmark, lambda _description: None)


@pytest.mark.asyncio
async def test_runner_fingerprints_config_without_redacting_token_limits():
    benchmark = Benchmark(name="fingerprint", tasks=[
        BenchmarkTask(id="one", description="first", metadata={"level": 1}),
    ])

    async def run_task(_description):
        return AgentResult(status=ResultStatus.SUCCESS, output="ok")

    config = {
        "provider": "fixture",
        "max_tokens": 4096,
        "prompt_tokens": 120,
        "api_key": "top-secret",
        "nested": {"github_token": "also-secret"},
    }
    first = await BenchmarkRunner().run(
        benchmark, run_task, evaluation_config=config,
        metadata={"repeat": 99},
    )
    second = await BenchmarkRunner().run(
        benchmark, run_task, evaluation_config=dict(config),
    )
    changed = await BenchmarkRunner().run(
        benchmark, run_task, evaluation_config={**config, "max_tokens": 8192},
    )

    effective = first.reproducibility["effective_config"]
    assert effective["max_tokens"] == 4096
    assert effective["prompt_tokens"] == 120
    assert effective["api_key"] == "<redacted>"
    assert effective["nested"]["github_token"] == "<redacted>"
    assert first.reproducibility["config_fingerprint"] == second.reproducibility["config_fingerprint"]
    assert first.reproducibility["config_fingerprint"] != changed.reproducibility["config_fingerprint"]
    assert first.metadata["repeat"] == 1


@pytest.mark.asyncio
async def test_runner_reports_agent_false_successes():
    benchmark = Benchmark(
        name="false-success",
        tasks=[BenchmarkTask(id="one", description="one")],
        grader=lambda _task, _result, _score: TaskGrade(False, 0.0, "grader failed"),
    )

    async def run_task(_description):
        return AgentResult(status=ResultStatus.SUCCESS, output="claimed done")

    result = await BenchmarkRunner().run(benchmark, run_task)
    assert result.agent_reported_successes == 1
    assert result.false_successes == 1
    assert result.verified_agent_reported_successes == 1
    assert result.false_success_rate == 1.0
    assert result.unverified_attempts == 0


def test_task_grade_rejects_non_finite_scores():
    with pytest.raises(ValueError, match="finite"):
        TaskGrade(True, float("nan"))


@pytest.mark.asyncio
async def test_runner_does_not_report_unverified_or_broken_grader_as_false_success():
    task = BenchmarkTask(id="one", description="one")

    async def run_task(_description):
        return AgentResult(status=ResultStatus.SUCCESS, output="claimed done")

    unverified = await BenchmarkRunner().run(Benchmark(name="none", tasks=[task]), run_task)
    assert unverified.false_successes == 0
    assert unverified.verified_agent_reported_successes == 0
    assert unverified.false_success_rate is None
    assert unverified.unverified_attempts == 1
    assert unverified.results[0].verification_status == "agent_status_only"
    assert unverified.results[0].passed is False
    assert unverified.scored_attempt_total == 0
    assert unverified.scored_task_total == 0

    def broken_grader(_task, _result, _score):
        raise RuntimeError("grader unavailable")

    broken = await BenchmarkRunner().run(
        Benchmark(name="broken", tasks=[task], grader=broken_grader), run_task,
    )
    assert broken.false_successes == 0
    assert broken.grader_error_attempts == 1
    assert broken.results[0].verification_status == "grader_error"
    assert broken.attempt_total == 1
    assert broken.scored_attempt_total == 0
    assert broken.excluded_attempts == 1
    assert broken.task_total == 1
    assert broken.scored_task_total == 0
    assert broken.by_category["uncategorized"]["excluded"] == 1


@pytest.mark.asyncio
async def test_runner_excludes_execution_errors_from_functional_denominators() -> None:
    benchmark = Benchmark(
        name="execution-error",
        tasks=[BenchmarkTask(id="one", description="one")],
    )

    async def run_task(_description):
        raise RuntimeError("provider unavailable")

    result = await BenchmarkRunner().run(benchmark, run_task)

    assert result.results[0].verification_status == "not_graded"
    assert result.attempt_total == 1
    assert result.scored_attempt_total == 0
    assert result.excluded_attempts == 1
    assert result.task_total == 1
    assert result.scored_task_total == 0
    assert result.attempt_passed == 0
    assert result.task_succeeded == 0


@pytest.mark.asyncio
async def test_runner_excludes_incomplete_repeat_sets_from_task_metrics() -> None:
    benchmark = Benchmark(
        name="incomplete-repeat",
        tasks=[BenchmarkTask(id="one", description="one")],
        grader=lambda _task, _result, _score: TaskGrade(True, 1.0),
    )
    calls = 0

    async def run_task(_description):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("provider unavailable")
        return AgentResult(status=ResultStatus.SUCCESS, output="ok")

    result = await BenchmarkRunner().run(benchmark, run_task, repeat=3)

    assert result.scored_attempt_total == 2
    assert result.attempt_passed == 2
    assert result.scored_task_total == 0
    assert result.task_succeeded == 0
    assert result.task_success_k == 3
    assert result.pass_at_k_by_k == {}
    assert result.pass_power_k_by_k == {}


@pytest.mark.asyncio
async def test_runner_marks_partial_efficiency_instead_of_understating_per_success_cost():
    benchmark = Benchmark(name="partial", tasks=[BenchmarkTask(id="one", description="one")])
    calls = 0

    async def run_task(_description):
        nonlocal calls
        calls += 1
        score = None if calls == 2 else {
            "efficiency": {
                "tokens_input": 10,
                "tokens_output": 5,
                "token_count_source": "exact",
                "cost_estimate_usd": 0.01,
            },
        }
        return AgentResult(status=ResultStatus.SUCCESS, output="ok"), score

    result = await BenchmarkRunner().run(benchmark, run_task, repeat=2)
    assert result.efficiency_provenance["token_coverage"] == 0.5
    assert result.efficiency_provenance["token_metrics_complete"] is False
    assert result.efficiency_provenance["token_count_sources"] == ["exact", "missing"]
    assert result.tokens_per_passed_attempt is None
    assert result.cost_per_passed_attempt_usd is None


@pytest.mark.asyncio
async def test_runner_dataset_manifest_is_portable_and_explicit():
    benchmark = Benchmark(
        name="manifest",
        tasks=[BenchmarkTask(
            id="one",
            description="one",
            metadata={"grader_command": ["pytest", "-q"], "timeout": 30},
        )],
        grader=lambda _task, _result, _score: TaskGrade(True, 1.0),
        metadata={"dataset_manifest": {"version": "v1", "source": "fixture", "license": "MIT"}},
    )

    async def run_task(_description):
        return AgentResult(status=ResultStatus.SUCCESS, output="ok")

    result = await BenchmarkRunner().run(
        benchmark,
        run_task,
        evaluation_config={
            "evaluation": {
                "dataset": {"name": "tasks.jsonl", "sha256": "abc"},
                "max_tasks": 1,
            },
        },
    )
    manifest = result.reproducibility["dataset_manifest"]
    assert manifest["version"] == "v1"
    assert manifest["source"] == "fixture"
    assert manifest["license"] == "MIT"
    assert manifest["dataset_file"] == {"name": "tasks.jsonl", "sha256": "abc"}
    assert manifest["grader_commands"][0]["type"] == "argv"
    assert manifest["grader_commands"][0]["argv_count"] == 2
    assert len(manifest["grader_commands"][0]["sha256"]) == 64
    assert manifest["grader_timeouts_seconds"] == [30]
    assert len(manifest["taskset_sha256"]) == 64
    assert len(manifest["grader_sha256"]) == 64
    assert result.mean_score_ci95 == [0.0, 1.0]
    assert "pytest" not in json.dumps(result.to_dict())


@pytest.mark.asyncio
async def test_runner_manifest_fingerprints_the_effective_grader_override():
    def original(_task, _result, _score):
        return TaskGrade(False, 0.0)

    def override(_task, _result, _score):
        return TaskGrade(True, 1.0)

    benchmark = Benchmark(
        name="override",
        tasks=[BenchmarkTask(id="one", description="one")],
        grader=original,
    )

    async def run_task(_description):
        return AgentResult(status=ResultStatus.SUCCESS, output="ok")

    original_result = await BenchmarkRunner().run(benchmark, run_task)
    result = await BenchmarkRunner().run(benchmark, run_task, grade_task=override)
    original_manifest = original_result.reproducibility["dataset_manifest"]
    manifest = result.reproducibility["dataset_manifest"]
    assert manifest["grader"].endswith("override")
    assert original_manifest["grader"].endswith("original")
    assert manifest["grader_sha256"] != original_manifest["grader_sha256"]


@pytest.mark.asyncio
async def test_runner_manifest_fingerprints_grader_helpers(monkeypatch):
    benchmark = Benchmark(
        name="helper",
        tasks=[BenchmarkTask(id="one", description="one")],
        grader=_HelperBackedGrader.grade,
    )

    async def run_task(_description):
        return AgentResult(status=ResultStatus.SUCCESS, output="ok")

    before = await BenchmarkRunner().run(benchmark, run_task)
    monkeypatch.setattr(_HelperBackedGrader, "execute", staticmethod(lambda: False))
    after = await BenchmarkRunner().run(benchmark, run_task)

    before_manifest = before.reproducibility["dataset_manifest"]
    after_manifest = after.reproducibility["dataset_manifest"]
    assert any(
        ".execute" in component["name"]
        for component in before_manifest["grader_components"]
    )
    assert before_manifest["grader_sha256"] != after_manifest["grader_sha256"]


@pytest.mark.asyncio
async def test_runner_manifest_records_explicit_grader_version_artifact_and_callable_name(
    tmp_path,
):
    artifact_sha256 = "a" * 64

    def original(_task, _result, _score):
        return TaskGrade(False, 0.0)

    def override(_task, _result, _score):
        return TaskGrade(True, 1.0)

    benchmark = Benchmark(
        name="versioned",
        tasks=[BenchmarkTask(id="one", description="one")],
        grader=original,
        metadata={
            "dataset_manifest": {
                "grader": "declared-original",
                "grader_version": "2026.08",
                "grader_artifacts": [{"digest": f"sha256:{artifact_sha256}"}],
                "source": "https://user:password@example.com/grader?token=secret",
            },
        },
    )

    async def run_task(_description):
        return AgentResult(status=ResultStatus.SUCCESS, output="ok")

    report = tmp_path / "versioned.json"
    result = await BenchmarkRunner().run(
        benchmark, run_task, grade_task=override, report_path=report,
    )
    manifest = result.reproducibility["dataset_manifest"]
    assert manifest["grader"].endswith("override")
    assert manifest["grader_label"] == "declared-original"
    assert manifest["grader_version"] == "2026.08"
    assert manifest["grader_artifact_sha256"] == [artifact_sha256]
    serialized = report.read_text(encoding="utf-8")
    assert "user:password" not in serialized
    assert "token=secret" not in serialized
    assert "https://example.com/grader" in serialized
