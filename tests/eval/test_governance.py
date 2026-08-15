from __future__ import annotations

import json
from copy import deepcopy

import pytest

from synapse.eval.governance import (
    ArtifactStore,
    ExperimentPreregistration,
    FrozenDatasetManifest,
    GraderCalibration,
    RunRegistry,
    TrendAnalyzer,
)
from synapse.eval.runner import Benchmark, BenchmarkRunner, BenchmarkTask, TaskGrade
from synapse.protocols.planner import AgentResult, ResultStatus


def test_freeze_manifest_is_content_bound_and_immutable(tmp_path):
    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text(
        '\n'.join([
            json.dumps({"id": "dev-1", "split": "dev", "repository_id": "r1"}),
            json.dumps({"id": "holdout-1", "split": "holdout", "repository_id": "r2"}),
        ]), encoding="utf-8",
    )
    manifest = FrozenDatasetManifest.freeze(
        dataset, name="fixture", version="1", source="local", license="MIT",
        grader_version="grader-1", image_digest="sha256:image",
        tombstones=[{"task_id": "dev-1", "reason": "retired"}],
    )
    target = manifest.write(tmp_path / "manifest.json")
    payload = json.loads(target.read_text(encoding="utf-8"))

    assert payload["split"]["holdout"] == ["holdout-1"]
    assert len(payload["manifest_sha256"]) == 64
    assert FrozenDatasetManifest.read(target).verify_dataset(dataset)
    assert manifest.write(target) == target
    replacement = FrozenDatasetManifest.freeze(
        dataset, name="fixture", version="2", source="local", license="MIT",
        grader_version="grader-1", image_digest="sha256:image",
    )
    with pytest.raises(FileExistsError, match="immutable"):
        replacement.write(target)


def test_preregistration_enforces_power_and_required_contract(tmp_path):
    prereg = ExperimentPreregistration(
        experiment_id="exp-1", primary_metric="task_success_rate",
        direction="higher", mde=0.2, alpha=0.05, power=0.8,
        baseline_rate=0.5, task_count=100, repeat=5,
        guardrails={"false_success_rate": {"max": 0.02}},
        stopping_rules=["stop on sandbox escape"],
        model_harness_matrix=[{"model": "m1", "harness": "synapse"}],
        dataset_manifest_sha256="a" * 64,
    )
    payload = prereg.to_dict()
    assert payload["estimated_minimum_task_count"] <= 100
    prereg.write(tmp_path / "prereg.json")
    with pytest.raises(FileExistsError):
        prereg.write(tmp_path / "prereg.json")


def test_grader_calibration_reports_false_accept_reject_and_errors():
    cases = [
        {"id": "correct", "expected_pass": True, "actual": True, "mutation": "none"},
        {"id": "near", "expected_pass": True, "actual": False, "mutation": "near_correct"},
        {"id": "deleted", "expected_pass": False, "actual": True, "mutation": "delete_tests"},
        {"id": "error", "expected_pass": False, "actual": "error", "mutation": "empty"},
    ]

    def grader(case):
        if case["actual"] == "error":
            raise RuntimeError("broken grader")
        return case["actual"]

    result = GraderCalibration.run(cases, grader)
    assert result.false_accepts == 1
    assert result.false_rejects == 1
    assert result.grader_errors == 1
    assert result.false_accept_rate == 1.0
    assert result.false_reject_rate == 0.5
    assert result.mutation_coverage["delete_tests"] == 1


@pytest.mark.asyncio
async def test_runner_archives_only_failed_attempts_by_content_address(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    benchmark = Benchmark(
        name="artifact",
        tasks=[BenchmarkTask(id="bad", description="bad")],
        grader=lambda _task, _result, _score: TaskGrade(False, 0, "failed"),
    )
    result = await BenchmarkRunner().run(
        benchmark,
        lambda _task: AgentResult(status=ResultStatus.SUCCESS, output="sensitive patch"),
        artifact_store=store,
    )
    reference = result.results[0].artifact_ref
    assert reference is not None and store.verify(reference)
    assert "sensitive patch" not in json.dumps(result.to_dict())


def test_registry_is_append_only_and_trend_checks_series(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    report = reports / "base.json"
    report.write_text(json.dumps({
        "task_success_rate": 0.8, "false_success_rate": 0.01,
        "p95_duration_ms": 100, "grader_error_attempts": 0,
        "safety_violation_rate": 0, "infrastructure_failure_rate": 0,
        "p95_tokens": 20,
        "reproducibility": {"dataset_manifest": {"taskset_sha256": "d"}},
    }), encoding="utf-8")
    registry = RunRegistry(tmp_path / "registry")
    base = registry.register(
        report, report_id="run-1", series="s1", role="baseline",
        status="complete", approval="approved",
    )
    with pytest.raises(FileExistsError):
        registry.register(
            report, report_id="run-1", series="s1", role="candidate",
            status="complete", approval="pending",
        )
    assert registry.verify(reports) == [{"report_id": "run-1", "valid": True}]
    assert registry.verify() == [{"report_id": "run-1", "valid": True}]
    trend = TrendAnalyzer.analyze([base])
    assert trend["comparable"] is True
    assert trend["alerts"] == []
    with pytest.raises(ValueError, match="registered baseline"):
        RunRegistry(tmp_path / "other").register(
            report, report_id="candidate-1", series="s1", role="candidate",
            status="complete", approval="pending", baseline_report_id="missing",
        )
    drifted = deepcopy(base)
    drifted["report_id"] = "run-2"
    drifted["fingerprints"]["dataset"] = "changed"
    incompatible = TrendAnalyzer.analyze([base, drifted])
    assert incompatible["comparable"] is False
    assert incompatible["issues"] == ["dataset fingerprint changed within series"]


@pytest.mark.asyncio
async def test_repository_cluster_bootstrap_is_selected_when_metadata_is_complete():
    benchmark = Benchmark(
        name="repos",
        tasks=[
            BenchmarkTask(id="a", description="a", metadata={"repository_id": "r1"}),
            BenchmarkTask(id="b", description="b", metadata={"repository_id": "r2"}),
        ],
        grader=lambda _task, _result, _score: TaskGrade(True, 1),
    )
    async def run_task(_task):
        return AgentResult(status=ResultStatus.SUCCESS, output="ok")

    result = await BenchmarkRunner().run(benchmark, run_task)
    assert result.inference_cluster == "repository"
    assert result.repository_cluster_count == 2
    assert result.task_success_rate_ci95 == [1.0, 1.0]
