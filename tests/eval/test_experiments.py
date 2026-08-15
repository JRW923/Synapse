"""Tests for paired A/B experiments."""

import json
from pathlib import Path

import pytest

from synapse.eval.experiments import Experiment, ExperimentResult
from synapse.eval.runner import Benchmark, BenchmarkTask, TaskGrade
from synapse.protocols.planner import AgentResult, ResultStatus


class _CountingBenchmark:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._counter = 0

    async def __call__(self, config: dict) -> float:
        self._counter += 1
        self.calls.append(config.get("label", "unknown"))
        return float(self._counter)


@pytest.mark.asyncio
async def test_legacy_float_callback_and_fields_remain_supported() -> None:
    benchmark = _CountingBenchmark()
    experiment = Experiment(
        id="exp-001",
        name="legacy",
        variables={},
        agent_config_a={"label": "A"},
        agent_config_b={"label": "B"},
        benchmark=benchmark,
        runs_per_config=5,
        seed=7,
        bootstrap_samples=200,
    )

    result = await experiment.run()

    assert result.experiment_id == "exp-001"
    assert result.experiment_name == "legacy"
    assert len(result.metrics_a) == 5
    assert len(result.metrics_b) == 5
    assert result.primary_metric == "metric"
    assert result.all_metrics_a == {"metric": result.metrics_a}
    assert result.all_metrics_b == {"metric": result.metrics_b}
    assert len([call for call in benchmark.calls if call == "A"]) == 5
    assert len([call for call in benchmark.calls if call == "B"]) == 5
    assert all(
        set(benchmark.calls[index:index + 2]) == {"A", "B"}
        for index in range(0, len(benchmark.calls), 2)
    )


@pytest.mark.asyncio
async def test_seed_makes_interleaving_and_round_seeds_reproducible() -> None:
    class SeededBenchmark:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        async def __call__(self, config: dict, seed: int) -> float:
            self.calls.append((config["label"], seed))
            return float(config["value"])

    first = SeededBenchmark()
    second = SeededBenchmark()

    def make(benchmark: SeededBenchmark) -> Experiment:
        return Experiment(
            id="seeded",
            name="seeded",
            variables={},
            agent_config_a={"label": "A", "value": 1},
            agent_config_b={"label": "B", "value": 2},
            benchmark=benchmark,
            runs_per_config=6,
            seed=42,
            bootstrap_samples=100,
        )

    result_a = await make(first).run()
    result_b = await make(second).run()

    assert first.calls == second.calls
    assert result_a.run_order == result_b.run_order
    assert result_a.round_seeds == result_b.round_seeds
    for index in range(0, len(first.calls), 2):
        pair = first.calls[index:index + 2]
        assert {label for label, _ in pair} == {"A", "B"}
        assert pair[0][1] == pair[1][1]


def test_lower_is_better_uses_paired_randomization() -> None:
    result = ExperimentResult(
        experiment_id="exp-002",
        experiment_name="lower",
        metrics_a=[1.0, 1.1, 0.9, 1.0, 1.1, 0.9],
        metrics_b=[5.0, 5.1, 4.9, 5.0, 5.1, 4.9],
        bootstrap_samples=300,
    )

    assert result.p_value == pytest.approx(0.03125)
    assert result.winner == "A"
    assert result.paired_deltas == pytest.approx([4.0] * 6)
    assert result.mean_delta == pytest.approx(4.0)
    assert result.relative_change == pytest.approx(4.0)
    assert result.relative_improvement == pytest.approx(-4.0)
    assert result.bootstrap_ci == pytest.approx((4.0, 4.0))


def test_identical_pairs_have_no_winner() -> None:
    result = ExperimentResult(
        experiment_id="exp-003",
        experiment_name="same",
        metrics_a=[1.0, 1.1, 0.9, 1.0, 1.1],
        metrics_b=[1.0, 1.1, 0.9, 1.0, 1.1],
        bootstrap_samples=100,
    )

    assert result.p_value == 1.0
    assert result.winner is None
    assert result.mean_delta == 0.0


@pytest.mark.asyncio
async def test_dict_metrics_primary_direction_and_paired_statistics(tmp_path) -> None:
    def workspace_factory(*, label: str, task_id: str, attempt: int) -> dict:
        workspace = tmp_path / f"{task_id}-{attempt}-{label}"
        workspace.mkdir()
        return {"path": workspace, "baseline_id": task_id}

    async def benchmark(config: dict, *, seed: int):
        assert isinstance(seed, int)
        return (
            {
                "success_rate": config["success_rate"],
                "latency_ms": config["latency_ms"],
            },
            {"comparability": {
                "source": "runner",
                "model_id": "fixture-model",
                "budgets": {"max_tokens": 1000},
                "permissions": {"network": False},
            }},
        )

    result = await Experiment(
        id="multi",
        name="multi-metric",
        variables={},
        agent_config_a={"success_rate": 0.5, "latency_ms": 100.0},
        agent_config_b={"success_rate": 0.9, "latency_ms": 80.0},
        benchmark=benchmark,
        runs_per_config=6,
        primary_metric="success_rate",
        direction="higher",
        metric_directions={"success_rate": "higher", "latency_ms": "lower"},
        seed=11,
        bootstrap_samples=300,
        allowed_config_diff_paths=("latency_ms", "success_rate"),
        workspace_factory=workspace_factory,
    ).run()

    assert set(result.metric_results) == {"success_rate", "latency_ms"}
    assert result.metrics_a == [0.5] * 6
    assert result.metrics_b == [0.9] * 6
    assert result.winner == "B"
    assert result.p_value == pytest.approx(0.03125)
    assert result.mean_delta == pytest.approx(0.4)
    assert result.relative_change == pytest.approx(0.8)
    assert result.relative_improvement == pytest.approx(0.8)
    assert result.bootstrap_ci == pytest.approx((0.4, 0.4))
    assert result.metric_results["latency_ms"].mean_delta == pytest.approx(-20.0)
    assert result.metric_results["latency_ms"].relative_improvement == pytest.approx(0.2)
    assert result.comparability_eligible is True


def test_higher_is_better_remains_a_compatibility_alias() -> None:
    result = ExperimentResult(
        metrics_a=[0.0] * 6,
        metrics_b=[1.0] * 6,
        higher_is_better=True,
        bootstrap_samples=100,
    )

    assert result.direction == "higher"
    assert result.winner == "B"


def test_p_value_guard_prevents_small_sample_winner() -> None:
    result = ExperimentResult(
        metrics_a=[0.0] * 5,
        metrics_b=[1.0] * 5,
        direction="higher",
        bootstrap_samples=100,
    )

    assert result.bootstrap_ci == (1.0, 1.0)
    assert result.p_value == pytest.approx(0.0625)
    assert result.winner is None
    assert result.outcome == "inconclusive"


def test_ci_crossing_zero_is_inconclusive_even_with_custom_alpha() -> None:
    result = ExperimentResult(
        metrics_a=[0.0, 0.0, 0.0, 0.0],
        metrics_b=[-1.0, -1.0, 1.0, 1.0],
        direction="higher",
        alpha=1.0 - 1e-6,
        seed=3,
        bootstrap_samples=500,
    )

    assert result.bootstrap_ci is not None
    assert result.bootstrap_ci[0] < 0.0 < result.bootstrap_ci[1]
    assert result.winner is None


def test_bootstrap_interval_is_reproducible() -> None:
    kwargs = dict(
        metrics_a=[1.0, 2.0, 3.0, 4.0],
        metrics_b=[2.0, 2.5, 5.0, 7.0],
        seed=123,
        bootstrap_samples=500,
    )

    first = ExperimentResult(**kwargs)
    second = ExperimentResult(**kwargs)

    assert first.bootstrap_ci == second.bootstrap_ci
    assert first.bootstrap_ci is not None
    assert first.bootstrap_ci[0] <= first.mean_delta <= first.bootstrap_ci[1]


def test_guardrail_regression_turns_primary_winner_into_tradeoff() -> None:
    result = ExperimentResult(
        metrics_a={"success": [0.5] * 7, "safety_events": [0.0] * 7},
        metrics_b={"success": [1.0] * 7, "safety_events": [2.0] * 7},
        primary_metric="success",
        direction="higher",
        metric_directions={"success": "higher", "safety_events": "lower"},
        guardrail_metrics=("safety_events",),
        bootstrap_samples=100,
    )

    assert result.metric_results["success"].winner == "B"
    assert result.metric_results["safety_events"].winner == "A"
    assert result.winner is None
    assert result.outcome == "tradeoff"
    assert result.guardrail_regressions == ["safety_events"]


def test_primary_direction_cannot_be_overridden_by_metric_direction_map() -> None:
    result = ExperimentResult(
        metrics_a={"duration_ms": [10.0] * 6, "tokens": [100.0] * 6},
        metrics_b={"duration_ms": [20.0] * 6, "tokens": [90.0] * 6},
        primary_metric="duration_ms",
        direction="higher",
        metric_directions={"duration_ms": "lower", "tokens": "lower"},
        bootstrap_samples=100,
    )

    assert result.metric_results["duration_ms"].higher_is_better is True
    assert result.metric_directions["duration_ms"] == "higher"
    assert result.winner == "B"


def test_single_pair_is_inconclusive() -> None:
    result = ExperimentResult(
        metrics_a=[1.0],
        metrics_b=[2.0],
        direction="higher",
    )

    assert result.bootstrap_ci is None
    assert result.p_value is None
    assert result.winner is None
    assert result.outcome == "inconclusive"


def test_paired_metrics_reject_unmatched_observations() -> None:
    with pytest.raises(ValueError, match="same number"):
        ExperimentResult(metrics_a=[1.0, 2.0], metrics_b=[1.0])


@pytest.mark.parametrize("runs", [0, -1, 1.5, True])
def test_experiment_rejects_invalid_run_count(runs) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        Experiment(
            id="invalid-runs",
            name="invalid-runs",
            variables={},
            agent_config_a={},
            agent_config_b={},
            benchmark=lambda _config: 1.0,
            runs_per_config=runs,
        )


def test_experiment_preflight_rejects_unexpected_config_differences() -> None:
    with pytest.raises(ValueError, match="outside allowed paths"):
        Experiment(
            id="confounded",
            name="confounded",
            variables={},
            agent_config_a={"provider": "same", "eval_ablation": {"memory": True}},
            agent_config_b={"provider": "other", "eval_ablation": {"memory": False}},
            benchmark=lambda _config: 1.0,
            allowed_config_diff_paths=("eval_ablation.memory",),
        )

    experiment = Experiment(
        id="isolated",
        name="isolated",
        variables={},
        agent_config_a={"provider": "same", "eval_ablation": {"memory": True}},
        agent_config_b={"provider": "same", "eval_ablation": {"memory": False}},
        benchmark=lambda _config: 1.0,
        allowed_config_diff_paths=("eval_ablation.memory",),
    )
    assert experiment.allowed_config_diff_paths == ("eval_ablation.memory",)


@pytest.mark.asyncio
async def test_missing_comparison_contract_suppresses_winner(tmp_path) -> None:
    def workspace_factory(*, label: str, task_id: str, attempt: int) -> dict:
        workspace = tmp_path / f"{task_id}-{attempt}-{label}"
        workspace.mkdir()
        return {"path": workspace, "baseline_id": task_id}

    async def benchmark(config, _seed):
        return config["score"], {"comparability": {
            "source": "runner",
            "model_id": "fixture-model",
            "budgets": {"max_tokens": 1000},
            "permissions": {"network": False},
        }}

    result = await Experiment(
        id="missing-contract",
        name="missing-contract",
        variables={},
        agent_config_a={"score": 0.0},
        agent_config_b={"score": 1.0},
        benchmark=benchmark,
        runs_per_config=6,
        direction="higher",
        bootstrap_samples=100,
        workspace_factory=workspace_factory,
    ).run()

    assert result.metric_results["metric"].p_value == pytest.approx(0.03125)
    assert result.metric_results["metric"].winner is None
    assert result.winner is None
    assert result.comparability_eligible is False
    assert result.comparability_issues == ["allowed_config_diff_paths_missing"]
    assert result.to_dict()["comparability_issues"] == [
        "allowed_config_diff_paths_missing"
    ]


@pytest.mark.asyncio
async def test_unproven_workspace_isolation_suppresses_winner() -> None:
    async def benchmark(config, _seed):
        return config["score"], {"comparability": {
            "source": "runner",
            "model_id": "fixture-model",
            "budgets": {"max_tokens": 1000},
            "permissions": {"network": False},
        }}

    result = await Experiment(
        id="shared-workspace",
        name="shared-workspace",
        variables={},
        agent_config_a={"score": 0.0},
        agent_config_b={"score": 1.0},
        benchmark=benchmark,
        runs_per_config=6,
        direction="higher",
        bootstrap_samples=100,
        allowed_config_diff_paths=("score",),
    ).run()

    assert result.winner is None
    assert result.comparability_issues == ["workspace_isolation_unproven"]


@pytest.mark.asyncio
async def test_missing_runtime_evidence_suppresses_winner(tmp_path) -> None:
    def workspace_factory(*, label: str, task_id: str, attempt: int) -> dict:
        workspace = tmp_path / f"{task_id}-{attempt}-{label}"
        workspace.mkdir()
        return {"path": workspace, "baseline_id": task_id}

    async def benchmark(config, _seed):
        return config["score"]

    result = await Experiment(
        id="missing-runtime-evidence",
        name="missing-runtime-evidence",
        variables={},
        agent_config_a={"score": 0.0},
        agent_config_b={"score": 1.0},
        benchmark=benchmark,
        runs_per_config=6,
        direction="higher",
        bootstrap_samples=100,
        allowed_config_diff_paths=("score",),
        workspace_factory=workspace_factory,
    ).run()

    assert result.winner is None
    assert set(result.comparability_issues) == {
        "actual_model_id_missing",
        "budget_evidence_missing",
        "permission_evidence_missing",
    }


@pytest.mark.asyncio
async def test_protected_config_differences_suppress_winner(tmp_path) -> None:
    def workspace_factory(*, label: str, task_id: str, attempt: int) -> dict:
        workspace = tmp_path / f"{task_id}-{attempt}-{label}"
        workspace.mkdir()
        return {"path": workspace, "baseline_id": task_id}

    async def benchmark(config, _seed):
        return config["score"], {"comparability": {
            "source": "runner",
            "model_id": "fixture-model",
            "budgets": {"max_tokens": 1000},
            "permissions": {"network": False},
        }}

    result = await Experiment(
        id="protected-diff",
        name="protected-diff",
        variables={},
        agent_config_a={
            "score": 0.0,
            "budgets": {"max_tokens": 1000},
            "permissions": {"network": False},
        },
        agent_config_b={
            "score": 1.0,
            "budgets": {"max_tokens": 2000},
            "permissions": {"network": True},
        },
        benchmark=benchmark,
        runs_per_config=6,
        direction="higher",
        bootstrap_samples=100,
        allowed_config_diff_paths=(
            "budgets.max_tokens", "permissions.network", "score",
        ),
        workspace_factory=workspace_factory,
    ).run()

    assert result.winner is None
    assert set(result.comparability_issues) == {
        "protected_config_diff:budgets.max_tokens",
        "protected_config_diff:permissions.network",
    }


@pytest.mark.asyncio
async def test_metric_shape_must_stay_stable() -> None:
    calls = 0

    async def benchmark(_config: dict) -> dict[str, float]:
        nonlocal calls
        calls += 1
        return {"score": 1.0} if calls == 1 else {"other": 1.0}

    experiment = Experiment(
        id="invalid",
        name="invalid",
        variables={},
        agent_config_a={},
        agent_config_b={},
        benchmark=benchmark,
        runs_per_config=1,
        primary_metric="score",
    )

    with pytest.raises(ValueError, match="same metrics|primary metric"):
        await experiment.run()


@pytest.mark.asyncio
async def test_experiment_report_redacts_configs_and_is_json_safe(tmp_path) -> None:
    async def benchmark(config: dict) -> float:
        return float(config["value"])

    result = await Experiment(
        id="report",
        name="report",
        variables={
            "task": "fixture",
            "non_finite": float("nan"),
            "api_key": "variable-secret",
            "nested": {"authorization": "nested-secret"},
        },
        agent_config_a={"value": 1, "api_key": "secret-a"},
        agent_config_b={"value": 2, "nested": {"access_token": "secret-b"}},
        benchmark=benchmark,
        effective_config_a={"value": 1, "provider": {"api_key": "effective-a"}},
        effective_config_b={"value": 2, "provider": {"api_key": "effective-b"}},
        runs_per_config=2,
    ).run()

    payload = result.to_dict()
    assert payload["schema_version"] == 1
    assert payload["variables"]["task"] == "fixture"
    assert payload["variables"]["non_finite"] is None
    assert payload["variables"]["api_key"] == "<redacted>"
    assert payload["variables"]["nested"]["authorization"] == "<redacted>"
    json.dumps(payload, allow_nan=False)
    assert payload["config_a"]["api_key"] == "<redacted>"
    assert payload["config_b"]["nested"]["access_token"] == "<redacted>"
    assert len(payload["config_fingerprints"]["A"]) == 64
    assert payload["effective_config_a"]["provider"]["api_key"] == "<redacted>"
    assert len(payload["effective_config_fingerprints"]["B"]) == 64
    assert payload["config_diff_paths"] == ["value"]
    target = result.write_json(tmp_path / "experiment.json")
    serialized = target.read_text(encoding="utf-8")
    assert "variable-secret" not in serialized
    assert "nested-secret" not in serialized
    assert json.loads(serialized)["experiment_id"] == "report"

    html_target = result.write_html(tmp_path / "experiment.html")
    html_text = html_target.read_text(encoding="utf-8")
    assert "配对 A/B 实验报告 / Paired A/B experiment report" in html_text
    assert "指标比较 / Metric comparisons" in html_text
    assert "可比性 / Comparability" in html_text
    assert "secret-a" not in html_text
    assert "secret-b" not in html_text


@pytest.mark.asyncio
async def test_multitask_seed_reproduces_pair_order_and_external_grader_facts() -> None:
    def grade(task, result, _run_score):
        return TaskGrade(
            passed=result.output == "B",
            score=float(result.output == "B"),
            reason=task.id,
            details={"artifact": Path(f"{task.id}.txt")},
        )

    benchmark = Benchmark(
        name="paired",
        tasks=[
            BenchmarkTask(id="one", description="first"),
            BenchmarkTask(id="two", description="second"),
        ],
        grader=grade,
    )

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, int]] = []

        async def __call__(self, config, task, seed):
            self.calls.append((config["label"], task.id, seed))
            return AgentResult(ResultStatus.SUCCESS, config["label"])

    async def run(recorder):
        return await Experiment(
            id="tasks",
            name="tasks",
            variables={},
            agent_config_a={"label": "A"},
            agent_config_b={"label": "B"},
            benchmark=benchmark,
            task_runner=recorder,
            runs_per_config=2,
            seed=19,
            bootstrap_samples=100,
        ).run()

    first_recorder, second_recorder = Recorder(), Recorder()
    first, second = await run(first_recorder), await run(second_recorder)

    assert first_recorder.calls == second_recorder.calls
    assert first.run_order == second.run_order
    assert first.round_seeds == second.round_seeds
    assert first.task_count == 2
    assert first.attempt_pairs == 4
    for index, pair in enumerate(first.task_observations):
        calls = first_recorder.calls[index * 2:index * 2 + 2]
        assert {label for label, _, _ in calls} == {"A", "B"}
        assert {seed for _, _, seed in calls} == {pair.seed}
        assert tuple(label for label, _, _ in calls) == pair.run_order
        assert all(
            variant.verification_status == "verified"
            for variant in pair.variants.values()
        )
    artifact = first.to_dict()["task_observations"][0]["variants"]["A"][
        "grade_details"
    ]["artifact"]
    assert "one.txt" not in artifact
    assert "sha256=" in artifact


@pytest.mark.asyncio
async def test_runtime_model_mismatch_suppresses_task_winner(tmp_path) -> None:
    benchmark = Benchmark(
        name="model-mismatch",
        tasks=[
            BenchmarkTask(id=f"task-{index}", description="task")
            for index in range(6)
        ],
        grader=lambda _task, result, _score: TaskGrade(
            result.output == "B", float(result.output == "B"),
        ),
    )

    def workspace_factory(*, label: str, task_id: str, attempt: int) -> dict:
        workspace = tmp_path / f"{task_id}-{attempt}-{label}"
        workspace.mkdir()
        return {"path": workspace, "baseline_id": task_id}

    async def run_task(config, _task, _seed):
        return AgentResult(ResultStatus.SUCCESS, config["label"]), {
            "comparability": {
                "source": "runner",
                "model_id": f"model-{config['label']}",
                "budgets": {"max_tokens": 1000},
                "permissions": {"network": False},
            },
            "efficiency": {},
        }

    result = await Experiment(
        id="model-mismatch",
        name="model-mismatch",
        variables={},
        agent_config_a={"label": "A"},
        agent_config_b={"label": "B"},
        benchmark=benchmark,
        task_runner=run_task,
        runs_per_config=1,
        bootstrap_samples=100,
        allowed_config_diff_paths=("label",),
        workspace_factory=workspace_factory,
    ).run()

    comparison = result.metric_results["functional_success"]
    assert comparison.p_value == pytest.approx(0.03125)
    assert comparison.winner is None
    assert result.winner is None
    assert result.comparability_issues == ["actual_model_id_mismatch"]
    assert result.comparability_evidence["actual_model_ids"] == {
        "A": ["model-A"],
        "B": ["model-B"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mismatch_field", "expected_issue"),
    [("budgets", "budget_mismatch"), ("permissions", "permission_mismatch")],
)
async def test_runtime_budget_or_permission_mismatch_suppresses_winner(
    tmp_path, mismatch_field, expected_issue,
) -> None:
    def workspace_factory(*, label: str, task_id: str, attempt: int) -> dict:
        workspace = tmp_path / f"{task_id}-{attempt}-{label}"
        workspace.mkdir()
        return {"path": workspace, "baseline_id": task_id}

    async def benchmark(config, _seed):
        label = config["label"]
        budgets = {
            "max_tokens": 1000 + int(
                label == "B" and mismatch_field == "budgets"
            ),
        }
        permissions = {
            "network": label == "B" and mismatch_field == "permissions",
        }
        return {"duration_ms": 10.0 if label == "A" else 1.0}, {
            "comparability": {
                "source": "runner",
                "model_id": "fixture-model",
                "budgets": budgets,
                "permissions": permissions,
            },
        }

    result = await Experiment(
        id=f"{mismatch_field}-mismatch",
        name=f"{mismatch_field}-mismatch",
        variables={},
        agent_config_a={"label": "A"},
        agent_config_b={"label": "B"},
        benchmark=benchmark,
        runs_per_config=6,
        primary_metric="duration_ms",
        bootstrap_samples=100,
        allowed_config_diff_paths=("label",),
        workspace_factory=workspace_factory,
    ).run()

    assert result.metric_results["duration_ms"].p_value == pytest.approx(0.03125)
    assert result.metric_results["duration_ms"].winner is None
    assert result.winner is None
    assert result.comparability_issues == [expected_issue]


@pytest.mark.asyncio
async def test_workspace_factory_is_scoped_by_task_or_sequence(tmp_path) -> None:
    benchmark = Benchmark(
        name="workspace-groups",
        tasks=[
            BenchmarkTask(id="one", description="one"),
            BenchmarkTask(id="two", description="two"),
            BenchmarkTask(
                id="remember", description="remember",
                metadata={"sequence_id": "memory-sequence"},
            ),
            BenchmarkTask(
                id="recall", description="recall",
                metadata={"sequence_id": "memory-sequence"},
            ),
        ],
        grader=lambda _task, _result, _score: TaskGrade(True, 1.0),
    )
    factory_calls: list[tuple[str, str, int]] = []
    workspaces: dict[tuple[str, str], str] = {}

    def workspace_factory(*, label: str, task_id: str, attempt: int) -> dict:
        factory_calls.append((label, task_id, attempt))
        workspace = tmp_path / f"{task_id}-{attempt}-{label}"
        workspace.mkdir()
        return {"path": workspace, "baseline_id": task_id}

    async def run_task(config, task, _seed):
        workspaces[(config["label"], task.id)] = config["workspace_root"]
        return AgentResult(ResultStatus.SUCCESS, "ok"), {
            "comparability": {
                "source": "runner",
                "model_id": "fixture-model",
                "budgets": {"max_tokens": 1000},
                "permissions": {"network": False},
            },
            "efficiency": {},
        }

    result = await Experiment(
        id="workspace-groups",
        name="workspace-groups",
        variables={},
        agent_config_a={"label": "A"},
        agent_config_b={"label": "B"},
        benchmark=benchmark,
        task_runner=run_task,
        runs_per_config=1,
        allowed_config_diff_paths=("label",),
        workspace_factory=workspace_factory,
    ).run()

    assert result.comparability_eligible is True
    assert len(factory_calls) == 6
    for label in ("A", "B"):
        assert workspaces[(label, "one")] != workspaces[(label, "two")]
        assert workspaces[(label, "remember")] == workspaces[(label, "recall")]
    assert workspaces[("A", "one")] != workspaces[("B", "one")]


@pytest.mark.asyncio
async def test_no_grader_keeps_agent_status_diagnostic_and_never_selects_winner() -> None:
    benchmark = Benchmark(
        name="unverified",
        tasks=[
            BenchmarkTask(id="one", description="one"),
            BenchmarkTask(id="two", description="two"),
        ],
    )

    async def run_task(config, _task, _seed):
        return AgentResult(
            ResultStatus.SUCCESS if config["label"] == "B" else ResultStatus.FAILED,
            config["label"],
        )

    result = await Experiment(
        id="unverified",
        name="unverified",
        variables={},
        agent_config_a={"label": "A"},
        agent_config_b={"label": "B"},
        benchmark=benchmark,
        task_runner=run_task,
        runs_per_config=3,
        seed=3,
        bootstrap_samples=100,
    ).run()

    comparison = result.metric_results["agent_reported_success"]
    assert comparison.role == "diagnostic"
    assert comparison.winner is None
    assert comparison.adjusted_p_value is None
    assert result.winner is None
    assert result.outcome == "inconclusive"


@pytest.mark.asyncio
async def test_callback_agent_status_is_diagnostic_even_with_valid_contract(
    tmp_path,
) -> None:
    def workspace_factory(*, label: str, task_id: str, attempt: int) -> dict:
        workspace = tmp_path / f"{task_id}-{attempt}-{label}"
        workspace.mkdir()
        return {"path": workspace, "baseline_id": task_id}

    async def benchmark(config, _seed):
        return {"agent_reported_success": float(config["label"] == "B")}, {
            "comparability": {
                "source": "runner",
                "model_id": "fixture-model",
                "budgets": {"max_tokens": 1000},
                "permissions": {"network": False},
            },
        }

    result = await Experiment(
        id="callback-agent-status",
        name="callback-agent-status",
        variables={},
        agent_config_a={"label": "A"},
        agent_config_b={"label": "B"},
        benchmark=benchmark,
        runs_per_config=6,
        bootstrap_samples=100,
        allowed_config_diff_paths=("label",),
        workspace_factory=workspace_factory,
    ).run()

    comparison = result.metric_results["agent_reported_success"]
    assert result.comparability_eligible is True
    assert comparison.role == "diagnostic"
    assert comparison.p_value == pytest.approx(0.03125)
    assert comparison.adjusted_p_value is None
    assert comparison.winner is None
    assert result.winner is None


@pytest.mark.asyncio
async def test_no_grader_rejects_confirmatory_agent_status() -> None:
    benchmark = Benchmark(
        name="unverified",
        tasks=[BenchmarkTask(id="one", description="one")],
    )

    async def run_task(_config, _task, _seed):
        return AgentResult(ResultStatus.SUCCESS, "done")

    with pytest.raises(ValueError, match="cannot be confirmatory"):
        await Experiment(
            id="unverified",
            name="unverified",
            variables={},
            agent_config_a={"label": "A"},
            agent_config_b={"label": "B"},
            benchmark=benchmark,
            task_runner=run_task,
            inferential_metrics=("agent_reported_success",),
        ).run()


@pytest.mark.asyncio
async def test_sequence_runner_reuses_state_within_attempt_and_resets_between_attempts() -> None:
    benchmark = Benchmark(
        name="memory-sequence",
        tasks=[
            BenchmarkTask(id="remember", description="remember"),
            BenchmarkTask(id="recall", description="recall"),
        ],
        grader=lambda _task, _result, _score: TaskGrade(True, 1.0),
    )
    states: dict[tuple[str, int], list[str]] = {}

    async def run_task(config, task, _seed, attempt):
        state = states.setdefault((config["label"], attempt), [])
        if task.id == "recall":
            assert state == ["remember"]
        state.append(task.id)
        return AgentResult(ResultStatus.SUCCESS, "ok")

    result = await Experiment(
        id="memory-sequence",
        name="memory-sequence",
        variables={},
        agent_config_a={"label": "A"},
        agent_config_b={"label": "B"},
        benchmark=benchmark,
        task_runner=run_task,
        runs_per_config=2,
        seed=3,
        bootstrap_samples=10,
    ).run()

    assert states == {
        ("A", 1): ["remember", "recall"],
        ("B", 1): ["remember", "recall"],
        ("A", 2): ["remember", "recall"],
        ("B", 2): ["remember", "recall"],
    }
    assert [(item.task_id, item.attempt) for item in result.task_observations] == [
        ("remember", 1), ("recall", 1), ("remember", 2), ("recall", 2),
    ]


def test_continuous_metrics_bootstrap_over_task_clusters() -> None:
    result = ExperimentResult(
        metrics_a={"score": [0.0, 0.0, 0.0, 0.0]},
        metrics_b={"score": [4.0, 4.0, 4.0, -4.0]},
        primary_metric="score",
        direction="higher",
        metric_task_ids={"score": ["one", "one", "one", "two"]},
        bootstrap_samples=300,
        seed=7,
    )

    comparison = result.metric_results["score"]
    assert comparison.analysis_unit == "task"
    assert comparison.attempt_count == 4
    assert comparison.task_count == 2
    assert comparison.values_a == [0.0, 0.0]
    assert comparison.values_b == [4.0, -4.0]
    assert comparison.mean_delta == 0.0
    assert comparison.bootstrap_ci == (-4.0, 4.0)


def test_binary_functional_result_uses_exact_mcnemar() -> None:
    task_ids = [f"task-{index}" for index in range(6)]
    result = ExperimentResult(
        metrics_a={"functional_success": [0.0] * 6},
        metrics_b={"functional_success": [1.0] * 6},
        primary_metric="functional_success",
        direction="higher",
        metric_task_ids={"functional_success": task_ids},
        binary_metrics=("functional_success",),
        bootstrap_samples=100,
    )

    comparison = result.metric_results["functional_success"]
    assert comparison.test == "exact_mcnemar"
    assert comparison.mcnemar_counts == {"a_only": 0, "b_only": 6}
    assert comparison.p_value == pytest.approx(0.03125)
    assert comparison.winner == "B"


def test_multimetric_task_statistics_apply_holm_before_selecting_winner() -> None:
    task_ids = [f"task-{index}" for index in range(6)]
    result = ExperimentResult(
        metrics_a={"quality": [0.0] * 6, "efficiency": [0.0] * 6},
        metrics_b={"quality": [1.0] * 6, "efficiency": [1.0] * 6},
        primary_metric="quality",
        direction="higher",
        metric_directions={"efficiency": "higher"},
        metric_task_ids={"quality": task_ids, "efficiency": task_ids},
        inferential_metrics=("quality", "efficiency"),
        bootstrap_samples=100,
    )

    for comparison in result.metric_results.values():
        assert comparison.p_value == pytest.approx(0.03125)
        assert comparison.adjusted_p_value == pytest.approx(0.0625)
        assert comparison.winner is None
    assert result.adjusted_p_value == pytest.approx(0.0625)
    assert result.winner is None


@pytest.mark.asyncio
async def test_single_benchmark_task_is_inconclusive_after_repeats() -> None:
    task = BenchmarkTask(id="only", description="single task")

    async def run_task(config, _task, _seed):
        return AgentResult(ResultStatus.SUCCESS, config["label"])

    def grade(_task, result, _run_score):
        return TaskGrade(result.output == "B", float(result.output == "B"))

    result = await Experiment(
        id="single-task",
        name="single-task",
        variables={},
        agent_config_a={"label": "A"},
        agent_config_b={"label": "B"},
        benchmark=task,
        task_runner=run_task,
        grade_task=grade,
        runs_per_config=3,
        bootstrap_samples=100,
    ).run()

    assert result.metrics_a == [0.0, 0.0, 0.0]
    assert result.metrics_b == [1.0, 1.0, 1.0]
    assert result.metric_results["functional_success"].task_count == 1
    assert result.p_value is None
    assert result.winner is None


@pytest.mark.asyncio
async def test_grader_error_is_not_recorded_as_functional_failure() -> None:
    def grade(task, result, _run_score):
        if task.id == "broken" and result.output == "B":
            raise RuntimeError("grader unavailable")
        return TaskGrade(result.output == "B", float(result.output == "B"))

    benchmark = Benchmark(
        name="grader-errors",
        tasks=[
            BenchmarkTask(id="broken", description="broken grader"),
            BenchmarkTask(id="valid", description="valid grader"),
        ],
        grader=grade,
    )

    async def run_task(config, _task, _seed):
        return AgentResult(ResultStatus.SUCCESS, config["label"])

    result = await Experiment(
        id="grader-errors",
        name="grader-errors",
        variables={},
        agent_config_a={"label": "A"},
        agent_config_b={"label": "B"},
        benchmark=benchmark,
        task_runner=run_task,
        runs_per_config=1,
        bootstrap_samples=100,
    ).run()

    broken = result.task_observations[0].variants["B"]
    assert broken.verification_status == "grader_error"
    assert broken.functional_passed is None
    assert broken.grader_score is None
    assert broken.agent_reported_success is True
    assert result.all_metrics_a["functional_success"] == [0.0]
    assert result.all_metrics_b["functional_success"] == [1.0]
    assert result.metric_task_ids["functional_success"] == ["valid"]
    assert result.excluded_pair_count == 1
    assert result.excluded_pairs == [{
        "task_id": "broken",
        "attempt": 1,
        "reason": "incomplete_verified_pair",
        "verification_status": {"A": "verified", "B": "grader_error"},
    }]
    assert result.task_outcome_counts == {"excluded": 1, "improved": 1}
    assert result.failure_matrix == {
        "A": {"completion_false_positive": 2},
        "B": {"grader_error": 1},
    }
    json.dumps(result.to_dict())


@pytest.mark.asyncio
async def test_runner_error_does_not_turn_missing_efficiency_into_zero() -> None:
    benchmark = Benchmark(
        name="missing-efficiency",
        tasks=[
            BenchmarkTask(id="broken", description="broken"),
            BenchmarkTask(id="valid", description="valid"),
        ],
    )

    async def run_task(config, task, _seed):
        if task.id == "broken" and config["label"] == "A":
            raise RuntimeError("provider unavailable")
        return AgentResult(ResultStatus.SUCCESS, config["label"])

    result = await Experiment(
        id="missing-efficiency",
        name="missing-efficiency",
        variables={},
        agent_config_a={"label": "A"},
        agent_config_b={"label": "B"},
        benchmark=benchmark,
        task_runner=run_task,
        runs_per_config=1,
        bootstrap_samples=10,
    ).run()

    assert "tokens" not in result.metric_task_ids
    assert "tokens" not in result.all_metrics_a


@pytest.mark.asyncio
async def test_explicit_token_counts_without_source_remain_comparable() -> None:
    benchmark = Benchmark(
        name="legacy-token-source",
        tasks=[BenchmarkTask(id="task", description="task")],
    )

    async def run_task(config, _task, _seed):
        return (
            AgentResult(ResultStatus.SUCCESS, config["label"]),
            {
                "efficiency": {
                    "tokens_input": config["tokens"],
                    "tokens_output": 2,
                },
            },
        )

    result = await Experiment(
        id="legacy-token-source",
        name="legacy-token-source",
        variables={},
        agent_config_a={"label": "A", "tokens": 8},
        agent_config_b={"label": "B", "tokens": 4},
        benchmark=benchmark,
        task_runner=run_task,
        runs_per_config=1,
        primary_metric="tokens",
        bootstrap_samples=10,
    ).run()

    assert result.all_metrics_a["tokens"] == [10.0]
    assert result.all_metrics_b["tokens"] == [6.0]
    assert result.metric_task_ids["tokens"] == ["task"]


@pytest.mark.asyncio
async def test_incomplete_repeat_task_is_excluded_from_success_at_k() -> None:
    def grade(task, result, _run_score):
        if task.id == "flaky" and result.output == "B-2":
            raise RuntimeError("grader unavailable")
        passed = result.output.startswith("B-")
        return TaskGrade(passed, float(passed))

    benchmark = Benchmark(
        name="uniform-k",
        tasks=[
            BenchmarkTask(id="stable", description="stable"),
            BenchmarkTask(id="flaky", description="flaky"),
        ],
        grader=grade,
    )

    async def run_task(config, _task, _seed, attempt):
        return AgentResult(ResultStatus.SUCCESS, f"{config['label']}-{attempt}")

    result = await Experiment(
        id="uniform-k",
        name="uniform-k",
        variables={},
        agent_config_a={"label": "A"},
        agent_config_b={"label": "B"},
        benchmark=benchmark,
        task_runner=run_task,
        runs_per_config=2,
        bootstrap_samples=10,
    ).run()

    assert result.metric_task_ids["functional_success"] == ["stable", "stable"]
    assert result.all_metrics_a["functional_success"] == [0.0, 0.0]
    assert result.all_metrics_b["functional_success"] == [1.0, 1.0]
    assert result.metric_coverage["functional_success"] == {
        "scheduled_tasks": 2,
        "complete_tasks": 1,
        "excluded_tasks": 1,
        "scheduled_attempt_pairs": 4,
        "complete_attempt_pairs": 2,
    }
    assert result.task_outcome_counts["excluded"] == 1


@pytest.mark.asyncio
async def test_multitask_report_records_metric_roles_and_reproducibility() -> None:
    benchmark = Benchmark(
        name="reproducible",
        tasks=[BenchmarkTask(id="one", description="one")],
        grader=lambda _task, _result, _score: TaskGrade(True, 1.0),
        metadata={
            "dataset_manifest": {
                "version": "v1",
                "source": "fixture",
                "license": "MIT",
            },
        },
    )

    async def run_task(config, _task, _seed):
        return (
            AgentResult(ResultStatus.SUCCESS, "ok"),
            {
                "model_id": f"model-{config['label']}",
                "run_id": f"run-{config['label']}",
                "efficiency": {
                    "tokens_input": 8,
                    "tokens_output": 2,
                    "token_count_source": "exact",
                },
            },
        )

    result = await Experiment(
        id="reproducible",
        name="reproducible",
        variables={},
        agent_config_a={"label": "A"},
        agent_config_b={"label": "B"},
        benchmark=benchmark,
        task_runner=run_task,
        runs_per_config=1,
        bootstrap_samples=10,
    ).run()

    assert result.metric_results["functional_success"].role == "confirmatory"
    assert result.metric_results["tokens"].role == "diagnostic"
    assert result.metric_results["tokens"].adjusted_p_value is None
    assert result.reproducibility["dataset_manifest"]["version"] == "v1"
    assert result.reproducibility["actual_model_ids"] == {
        "A": ["model-A"],
        "B": ["model-B"],
    }
    assert result.reproducibility["actual_run_ids"] == {
        "A": ["run-A"],
        "B": ["run-B"],
    }
