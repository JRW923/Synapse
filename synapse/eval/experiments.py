"""Paired A/B experiments for agent configurations.

The public ``metrics_a`` / ``metrics_b`` / ``p_value`` / ``winner`` fields are
kept for callers of the original float-only API. Benchmarks may now return a
mapping of metric names to numeric values; the primary metric is mirrored into
the legacy fields.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import random
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any, Literal

from synapse.eval.reporting import redact_secrets, redact_text, sanitize_value


MetricOutput = int | float | Mapping[str, int | float]
Direction = Literal["higher", "lower"]
MetricRole = Literal["confirmatory", "exploratory", "diagnostic"]

_PROTECTED_COMPARISON_KEYS = {
    "budget",
    "budgets",
    "max_steps",
    "max_tokens",
    "model",
    "model_id",
    "permission",
    "permissions",
    "provider",
    "timeout",
    "timeout_seconds",
}
_TRUSTED_COMPARABILITY_SOURCES = {"runner", "harness_adapter"}


def _as_float(value: Any, metric: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"metric '{metric}' must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"metric '{metric}' must be finite")
    return number


def _normalize_output(value: MetricOutput, scalar_name: str) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {scalar_name: _as_float(value, scalar_name)}
    if not value:
        raise ValueError("benchmark metric mapping cannot be empty")
    metrics: dict[str, float] = {}
    for name, raw in value.items():
        key = str(name).strip()
        if not key:
            raise ValueError("benchmark metric names cannot be empty")
        metrics[key] = _as_float(raw, key)
    return metrics


def _percentile(values: list[float], quantile: float) -> float:
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1.0 - weight) + values[upper] * weight


def _paired_bootstrap_ci(
    deltas: list[float],
    *,
    confidence_level: float,
    samples: int,
    seed: int,
) -> tuple[float, float] | None:
    if len(deltas) < 2 or samples <= 0:
        return None
    rng = random.Random(seed)
    size = len(deltas)
    means = sorted(
        fmean(deltas[rng.randrange(size)] for _ in range(size))
        for _ in range(samples)
    )
    tail = (1.0 - confidence_level) / 2.0
    return (_percentile(means, tail), _percentile(means, 1.0 - tail))


def _paired_randomization_p_value(
    deltas: list[float],
    *,
    samples: int,
    seed: int,
) -> float | None:
    """Return a two-sided paired sign-flip randomization p-value."""
    if len(deltas) < 2:
        return None
    observed = abs(fmean(deltas))
    tolerance = 1e-12
    if observed <= tolerance:
        return 1.0

    size = len(deltas)
    if size <= 16:
        total = 1 << size
        extreme = 0
        for mask in range(total):
            randomized = sum(
                value if mask & (1 << index) else -value
                for index, value in enumerate(deltas)
            ) / size
            if abs(randomized) >= observed - tolerance:
                extreme += 1
        return extreme / total

    rng = random.Random(seed)
    draws = max(1, samples)
    extreme = 1  # include the observed assignment for an unbiased MC p-value
    for _ in range(draws):
        randomized = fmean(value if rng.getrandbits(1) else -value for value in deltas)
        if abs(randomized) >= observed - tolerance:
            extreme += 1
    return extreme / (draws + 1)


def _aggregate_task_pairs(
    values_a: list[float],
    values_b: list[float],
    task_ids: list[str],
    *,
    binary: bool,
) -> tuple[list[float], list[float]]:
    if len(task_ids) != len(values_a):
        raise ValueError("metric task ids must match paired observations")
    grouped: dict[str, tuple[list[float], list[float]]] = {}
    for task_id, value_a, value_b in zip(task_ids, values_a, values_b):
        task_a, task_b = grouped.setdefault(str(task_id), ([], []))
        task_a.append(value_a)
        task_b.append(value_b)
    aggregate = max if binary else fmean
    return (
        [float(aggregate(task_a)) for task_a, _ in grouped.values()],
        [float(aggregate(task_b)) for _, task_b in grouped.values()],
    )


def _exact_mcnemar_p_value(
    values_a: list[float], values_b: list[float]
) -> tuple[float | None, dict[str, int]]:
    """Return two-sided exact McNemar and its discordant task counts."""
    counts = {
        "a_only": sum(a == 1.0 and b == 0.0 for a, b in zip(values_a, values_b)),
        "b_only": sum(a == 0.0 and b == 1.0 for a, b in zip(values_a, values_b)),
    }
    if len(values_a) < 2:
        return None, counts
    discordant = counts["a_only"] + counts["b_only"]
    if discordant == 0:
        return 1.0, counts
    tail = min(counts.values())
    probability = sum(math.comb(discordant, index) for index in range(tail + 1))
    return min(1.0, 2.0 * probability / (2 ** discordant)), counts


def _holm_adjusted_p_values(
    values: Mapping[str, float | None],
) -> dict[str, float | None]:
    """Adjust a metric family while preserving metrics without a valid test."""
    valid = sorted(
        ((name, value) for name, value in values.items() if value is not None),
        key=lambda item: item[1],
    )
    adjusted: dict[str, float | None] = {name: None for name in values}
    previous = 0.0
    count = len(valid)
    for index, (name, value) in enumerate(valid):
        previous = min(1.0, max(previous, value * (count - index)))
        adjusted[name] = previous
    return adjusted


def _metric_winner(
    comparison: "MetricComparison", *, p_value: float | None, alpha: float
) -> str | None:
    interval = comparison.bootstrap_ci
    if (
        interval is None
        or p_value is None
        or p_value > alpha
        or comparison.mean_delta in (None, 0.0)
        or not (interval[0] > 0.0 or interval[1] < 0.0)
    ):
        return None
    b_is_better = (
        comparison.mean_delta > 0
        if comparison.higher_is_better
        else comparison.mean_delta < 0
    )
    return "B" if b_is_better else "A"


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    enum_value = getattr(value, "value", None)
    return _json_safe(enum_value) if enum_value is not None else str(value)


def _config_diff_paths(
    value_a: Mapping[str, Any], value_b: Mapping[str, Any], prefix: str = "",
) -> list[str]:
    paths: list[str] = []
    for key in sorted(set(value_a) | set(value_b), key=str):
        name = f"{prefix}.{key}" if prefix else str(key)
        item_a = value_a.get(key)
        item_b = value_b.get(key)
        if isinstance(item_a, Mapping) and isinstance(item_b, Mapping):
            paths.extend(_config_diff_paths(item_a, item_b, name))
        elif item_a != item_b:
            paths.append(name)
    return paths


def _protected_config_diff_paths(paths: list[str]) -> list[str]:
    return [
        path for path in paths
        if any(part.lower() in _PROTECTED_COMPARISON_KEYS for part in path.split("."))
    ]


def _evidence_fingerprint(value: Any) -> str:
    payload = json.dumps(
        _json_safe(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _runtime_comparability_evidence(value: Any) -> dict[str, Any]:
    """Extract runtime facts used to decide whether A/B results are comparable."""
    if not isinstance(value, Mapping):
        return {}
    envelope = value.get("comparability")
    if isinstance(envelope, Mapping):
        source = str(envelope.get("source", "")).strip()
        evidence: dict[str, Any] = {"source": source}
        if source not in _TRUSTED_COMPARABILITY_SOURCES:
            return evidence
        if isinstance(envelope.get("model_id"), str) and envelope["model_id"].strip():
            evidence["model_id"] = envelope["model_id"].strip()
        for target, aliases in (
            ("budgets", ("budgets", "budget")),
            ("permissions", ("permissions", "permission")),
        ):
            for alias in aliases:
                if alias in envelope:
                    evidence[target] = envelope[alias]
                    break
        return evidence
    for nested in value.values():
        if not isinstance(nested, Mapping):
            continue
        evidence = _runtime_comparability_evidence(nested)
        if evidence:
            return evidence
    return {}


def _mapping_number(value: Mapping[str, Any], key: str) -> float:
    raw = value.get(key, 0)
    return _as_float(raw, key) if raw is not None else 0.0


def _seed_call_style(callback: Any) -> str | None:
    """Return how an optional per-round seed can be passed to *callback*."""
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return None
    parameters = list(signature.parameters.values())
    if "seed" in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters
    ):
        return "keyword"
    positional = [
        parameter for parameter in parameters
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    if len(positional) >= 2 or any(
        parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters
    ):
        return "positional"
    return None


def _attempt_call_style(callback: Any) -> str | None:
    """Return how a task runner can receive its paired attempt number."""
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return None
    parameters = list(signature.parameters.values())
    if "attempt" in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters
    ):
        return "keyword"
    positional = [
        parameter for parameter in parameters
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    if len(positional) >= 4 or any(
        parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters
    ):
        return "positional"
    return None


@dataclass(frozen=True)
class VariantObservation:
    """One variant's execution and external-verification facts."""

    label: str
    metrics: dict[str, float]
    agent_status: str
    agent_reported_success: bool
    functional_passed: bool | None
    grader_score: float | None
    verification_status: str
    grade_reason: str = ""
    grade_details: dict[str, Any] = field(default_factory=dict)
    run_score: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True)
class TaskPairObservation:
    """A/B observations sharing one task, attempt number, and round seed."""

    task_id: str
    attempt: int
    seed: int
    run_order: tuple[str, str]
    variants: dict[str, VariantObservation]
    category: str = "uncategorized"


@dataclass(frozen=True)
class MetricComparison:
    """Paired statistics for one metric, with deltas defined as ``B - A``."""

    name: str
    values_a: list[float]
    values_b: list[float]
    paired_deltas: list[float]
    mean_a: float | None
    mean_b: float | None
    mean_delta: float | None
    relative_change: float | None
    relative_improvement: float | None
    bootstrap_ci: tuple[float, float] | None
    p_value: float | None
    adjusted_p_value: float | None
    winner: str | None
    higher_is_better: bool
    role: MetricRole = "exploratory"
    analysis_unit: str = "attempt"
    test: str = "paired_randomization"
    task_count: int = 0
    attempt_count: int = 0
    mcnemar_counts: dict[str, int] | None = None

    @property
    def bootstrap_ci95(self) -> tuple[float, float] | None:
        """Compatibility-friendly name for the default 95% interval."""
        return self.bootstrap_ci


def _compare_metric(
    name: str,
    values_a: list[float],
    values_b: list[float],
    *,
    higher_is_better: bool,
    confidence_level: float,
    bootstrap_samples: int,
    randomization_samples: int,
    alpha: float,
    seed: int,
    task_ids: list[str] | None = None,
    binary: bool = False,
    role: MetricRole = "exploratory",
) -> MetricComparison:
    if len(values_a) != len(values_b):
        raise ValueError("paired A/B metrics must contain the same number of observations")
    attempt_count = len(values_a)
    if task_ids is not None:
        paired_a, paired_b = _aggregate_task_pairs(
            values_a, values_b, task_ids, binary=binary
        )
    else:
        paired_a, paired_b = list(values_a), list(values_b)
    if binary and any(value not in {0.0, 1.0} for value in (*paired_a, *paired_b)):
        raise ValueError(f"binary metric '{name}' must contain only 0 or 1")
    deltas = [value_b - value_a for value_a, value_b in zip(paired_a, paired_b)]

    mean_a = fmean(paired_a) if paired_a else None
    mean_b = fmean(paired_b) if paired_b else None
    mean_delta = fmean(deltas) if deltas else None
    relative_change = None
    if mean_a not in (None, 0.0) and mean_delta is not None:
        relative_change = mean_delta / abs(mean_a)
    relative_improvement = None
    if relative_change is not None:
        relative_improvement = relative_change if higher_is_better else -relative_change

    bootstrap_ci = _paired_bootstrap_ci(
        deltas,
        confidence_level=confidence_level,
        samples=bootstrap_samples,
        seed=seed,
    )
    if binary:
        p_value, mcnemar_counts = _exact_mcnemar_p_value(paired_a, paired_b)
        test = "exact_mcnemar"
    else:
        p_value = _paired_randomization_p_value(
            deltas,
            samples=randomization_samples,
            seed=seed ^ 0x5DEECE66D,
        )
        mcnemar_counts = None
        test = "paired_randomization"

    comparison = MetricComparison(
        name=name,
        values_a=paired_a,
        values_b=paired_b,
        paired_deltas=deltas,
        mean_a=mean_a,
        mean_b=mean_b,
        mean_delta=mean_delta,
        relative_change=relative_change,
        relative_improvement=relative_improvement,
        bootstrap_ci=bootstrap_ci,
        p_value=p_value,
        adjusted_p_value=p_value,
        winner=None,
        higher_is_better=higher_is_better,
        role=role,
        analysis_unit="task" if task_ids is not None else "attempt",
        test=test,
        task_count=len(paired_a) if task_ids is not None else 0,
        attempt_count=attempt_count,
        mcnemar_counts=mcnemar_counts,
    )
    return replace(
        comparison,
        winner=(
            _metric_winner(comparison, p_value=p_value, alpha=alpha)
            if role == "confirmatory" else None
        ),
    )


@dataclass
class ExperimentResult:
    """Outcome of a paired A/B experiment.

    ``metrics_a`` and ``metrics_b`` remain lists containing the primary metric.
    Multi-metric raw values live in ``all_metrics_a`` / ``all_metrics_b`` and
    their statistics in ``metric_results``.
    """

    experiment_id: str = ""
    experiment_name: str = ""
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    variables: dict[str, Any] = field(default_factory=dict)
    config_a: dict[str, Any] = field(default_factory=dict)
    config_b: dict[str, Any] = field(default_factory=dict)
    config_fingerprints: dict[str, str] = field(default_factory=dict)
    effective_config_a: dict[str, Any] = field(default_factory=dict)
    effective_config_b: dict[str, Any] = field(default_factory=dict)
    effective_config_fingerprints: dict[str, str] = field(default_factory=dict)
    config_diff_paths: list[str] = field(default_factory=list)
    allowed_config_diff_paths: tuple[str, ...] | None = None
    metrics_a: list[float] | dict[str, list[float]] = field(default_factory=list)
    metrics_b: list[float] | dict[str, list[float]] = field(default_factory=list)
    primary_metric: str | None = None
    direction: Direction = "lower"
    higher_is_better: bool | None = None
    metric_directions: dict[str, Direction] = field(default_factory=dict)
    guardrail_metrics: tuple[str, ...] = ()
    inferential_metrics: tuple[str, ...] | None = None
    diagnostic_metrics: tuple[str, ...] = ()
    all_metrics_a: dict[str, list[float]] = field(default_factory=dict)
    all_metrics_b: dict[str, list[float]] = field(default_factory=dict)
    metric_task_ids: dict[str, list[str]] = field(default_factory=dict)
    binary_metrics: tuple[str, ...] = ()
    task_observations: list[TaskPairObservation] = field(default_factory=list)
    excluded_pairs: list[dict[str, Any]] = field(default_factory=list)
    metric_coverage: dict[str, dict[str, int]] = field(default_factory=dict)
    reproducibility: dict[str, Any] = field(default_factory=dict)
    comparability_eligible: bool = True
    comparability_issues: list[str] = field(default_factory=list)
    comparability_evidence: dict[str, Any] = field(default_factory=dict)
    seed: int = 0
    confidence_level: float = 0.95
    bootstrap_samples: int = 5000
    randomization_samples: int = 10000
    alpha: float = 0.05
    run_order: list[str] = field(default_factory=list)
    round_seeds: list[int] = field(default_factory=list)

    metric_results: dict[str, MetricComparison] = field(init=False, default_factory=dict)
    paired_deltas: list[float] = field(init=False, default_factory=list)
    mean_delta: float | None = field(init=False, default=None)
    relative_change: float | None = field(init=False, default=None)
    relative_improvement: float | None = field(init=False, default=None)
    bootstrap_ci: tuple[float, float] | None = field(init=False, default=None)
    p_value: float | None = field(init=False, default=None)
    adjusted_p_value: float | None = field(init=False, default=None)
    winner: str | None = field(init=False, default=None)
    outcome: str = field(init=False, default="inconclusive")
    guardrail_regressions: list[str] = field(init=False, default_factory=list)
    task_count: int = field(init=False, default=0)
    attempt_pairs: int = field(init=False, default=0)
    excluded_pair_count: int = field(init=False, default=0)
    task_comparisons: list[dict[str, Any]] = field(init=False, default_factory=list)
    task_outcome_counts: dict[str, int] = field(init=False, default_factory=dict)
    task_outcomes_by_category: dict[str, dict[str, int]] = field(
        init=False, default_factory=dict,
    )
    failure_matrix: dict[str, dict[str, int]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        if self.direction not in {"higher", "lower"}:
            raise ValueError("direction must be 'higher' or 'lower'")
        if self.higher_is_better is None:
            self.higher_is_better = self.direction == "higher"
        else:
            self.direction = "higher" if self.higher_is_better else "lower"
        if not 0.0 < self.confidence_level < 1.0:
            raise ValueError("confidence_level must be between 0 and 1")
        if not 0.0 < self.alpha < 1.0:
            raise ValueError("alpha must be between 0 and 1")

        if isinstance(self.metrics_a, dict) or isinstance(self.metrics_b, dict):
            if not isinstance(self.metrics_a, dict) or not isinstance(self.metrics_b, dict):
                raise TypeError("metrics_a and metrics_b must use the same shape")
            self.all_metrics_a = {
                str(name): [_as_float(value, str(name)) for value in values]
                for name, values in self.metrics_a.items()
            }
            self.all_metrics_b = {
                str(name): [_as_float(value, str(name)) for value in values]
                for name, values in self.metrics_b.items()
            }
        elif not self.all_metrics_a and not self.all_metrics_b:
            metric = self.primary_metric or "metric"
            self.all_metrics_a = {
                metric: [_as_float(value, metric) for value in self.metrics_a]
            }
            self.all_metrics_b = {
                metric: [_as_float(value, metric) for value in self.metrics_b]
            }
        else:
            self.all_metrics_a = {
                str(name): [_as_float(value, str(name)) for value in values]
                for name, values in self.all_metrics_a.items()
            }
            self.all_metrics_b = {
                str(name): [_as_float(value, str(name)) for value in values]
                for name, values in self.all_metrics_b.items()
            }

        if set(self.all_metrics_a) != set(self.all_metrics_b):
            raise ValueError("A and B must contain the same metric names")
        if self.primary_metric is None:
            self.primary_metric = next(iter(self.all_metrics_a), "metric")
        if self.primary_metric not in self.all_metrics_a:
            raise ValueError(f"primary metric '{self.primary_metric}' was not recorded")
        unknown_directions = set(self.metric_directions) - set(self.all_metrics_a)
        if unknown_directions:
            raise ValueError(f"metric directions reference unknown metrics: {sorted(unknown_directions)}")
        if any(value not in {"higher", "lower"} for value in self.metric_directions.values()):
            raise ValueError("metric directions must be 'higher' or 'lower'")
        unknown_guardrails = set(self.guardrail_metrics) - set(self.all_metrics_a)
        if unknown_guardrails:
            raise ValueError(f"guardrails reference unknown metrics: {sorted(unknown_guardrails)}")
        unknown_clusters = set(self.metric_task_ids) - set(self.all_metrics_a)
        if unknown_clusters:
            raise ValueError(f"task ids reference unknown metrics: {sorted(unknown_clusters)}")
        unknown_binary = set(self.binary_metrics) - set(self.all_metrics_a)
        if unknown_binary:
            raise ValueError(f"binary metrics reference unknown metrics: {sorted(unknown_binary)}")
        confirmatory = tuple(dict.fromkeys(
            self.inferential_metrics
            if self.inferential_metrics is not None
            else (self.primary_metric, *self.guardrail_metrics)
        ))
        if "agent_reported_success" in confirmatory:
            confirmatory = tuple(
                name for name in confirmatory if name != "agent_reported_success"
            )
        unknown_confirmatory = set(confirmatory) - set(self.all_metrics_a)
        if unknown_confirmatory:
            raise ValueError(
                f"inferential metrics reference unknown metrics: {sorted(unknown_confirmatory)}"
            )
        missing_guardrails = set(self.guardrail_metrics) - set(confirmatory)
        if missing_guardrails:
            raise ValueError(
                f"guardrail metrics must be confirmatory: {sorted(missing_guardrails)}"
            )
        diagnostics = tuple(dict.fromkeys(self.diagnostic_metrics))
        if "agent_reported_success" in self.all_metrics_a and (
            "agent_reported_success" not in diagnostics
        ):
            diagnostics = (*diagnostics, "agent_reported_success")
        if self.primary_metric not in confirmatory and self.primary_metric not in diagnostics:
            diagnostics = (*diagnostics, self.primary_metric)
        unknown_diagnostics = set(diagnostics) - set(self.all_metrics_a)
        if unknown_diagnostics:
            raise ValueError(
                f"diagnostic metrics reference unknown metrics: {sorted(unknown_diagnostics)}"
            )
        overlap = set(confirmatory) & set(diagnostics)
        if overlap:
            raise ValueError(
                f"metrics cannot be both confirmatory and diagnostic: {sorted(overlap)}"
            )
        self.inferential_metrics = confirmatory
        self.diagnostic_metrics = diagnostics
        self.metric_directions = dict(self.metric_directions)
        self.metric_directions[self.primary_metric] = self.direction
        self.metric_task_ids = {
            str(name): [str(task_id) for task_id in task_ids]
            for name, task_ids in self.metric_task_ids.items()
        }
        self.binary_metrics = tuple(dict.fromkeys(self.binary_metrics))

        self.metrics_a = list(self.all_metrics_a[self.primary_metric])
        self.metrics_b = list(self.all_metrics_b[self.primary_metric])
        for index, name in enumerate(sorted(self.all_metrics_a)):
            metric_direction = (
                self.direction if name == self.primary_metric
                else self.metric_directions.get(name, self.direction)
            )
            role: MetricRole = (
                "confirmatory" if name in confirmatory
                else "diagnostic" if name in diagnostics
                else "exploratory"
            )
            self.metric_results[name] = _compare_metric(
                name,
                self.all_metrics_a[name],
                self.all_metrics_b[name],
                higher_is_better=metric_direction == "higher",
                confidence_level=self.confidence_level,
                bootstrap_samples=max(0, int(self.bootstrap_samples)),
                randomization_samples=max(1, int(self.randomization_samples)),
                alpha=self.alpha,
                seed=self.seed + index * 1_000_003,
                task_ids=self.metric_task_ids.get(name),
                binary=name in self.binary_metrics,
                role=role,
            )

        if confirmatory:
            adjusted = _holm_adjusted_p_values({
                name: self.metric_results[name].p_value for name in confirmatory
            })
            for name in confirmatory:
                comparison = self.metric_results[name]
                adjusted_p = adjusted[name]
                self.metric_results[name] = replace(
                    comparison,
                    adjusted_p_value=adjusted_p,
                    winner=_metric_winner(
                        comparison, p_value=adjusted_p, alpha=self.alpha
                    ),
                )
        for name, comparison in tuple(self.metric_results.items()):
            if name not in confirmatory:
                self.metric_results[name] = replace(
                    comparison, adjusted_p_value=None, winner=None,
                )

        primary = self.metric_results[self.primary_metric]
        self.paired_deltas = list(primary.paired_deltas)
        self.mean_delta = primary.mean_delta
        self.relative_change = primary.relative_change
        self.relative_improvement = primary.relative_improvement
        self.bootstrap_ci = primary.bootstrap_ci
        self.p_value = primary.p_value
        self.adjusted_p_value = primary.adjusted_p_value
        self.winner = primary.winner
        self.task_count = len({
            observation.task_id for observation in self.task_observations
        }) or primary.task_count
        self.attempt_pairs = len(self.task_observations)
        self.excluded_pair_count = len(self.excluded_pairs)
        self._build_task_diagnostics()
        if self.winner is not None:
            self.guardrail_regressions = [
                name for name in self.guardrail_metrics
                if self.metric_results[name].winner not in {None, self.winner}
            ]
            if self.guardrail_regressions:
                self.winner = None
                self.outcome = "tradeoff"
            else:
                self.outcome = primary.winner
        if not self.comparability_eligible:
            self.comparability_issues = list(dict.fromkeys(
                self.comparability_issues or ["comparison_contract_unverified"]
            ))
            for name in confirmatory:
                self.metric_results[name] = replace(
                    self.metric_results[name], winner=None,
                )
            self.winner = None
            self.guardrail_regressions = []
            self.outcome = "inconclusive"

    @staticmethod
    def _failure_category(observation: VariantObservation) -> str | None:
        if observation.verification_status == "grader_error":
            return "grader_error"
        if observation.error:
            return "execution_error"
        run_score = observation.run_score or {}
        external = run_score.get("external_harness")
        if isinstance(external, Mapping):
            error = external.get("error")
            if isinstance(error, Mapping) and error.get("category"):
                return str(error["category"])
        if observation.functional_passed is False:
            return (
                "completion_false_positive"
                if observation.agent_reported_success
                else "functional_failure_unclassified"
            )
        if not observation.agent_reported_success:
            return "agent_reported_failure"
        return None

    def _build_task_diagnostics(self) -> None:
        grouped: dict[str, list[TaskPairObservation]] = {}
        for pair in self.task_observations:
            grouped.setdefault(pair.task_id, []).append(pair)
            for label, observation in pair.variants.items():
                category = self._failure_category(observation)
                if category is not None:
                    bucket = self.failure_matrix.setdefault(label, {})
                    bucket[category] = bucket.get(category, 0) + 1

        for task_id, pairs in grouped.items():
            verified = [
                pair for pair in pairs
                if all(
                    pair.variants[label].functional_passed is not None
                    for label in ("A", "B")
                )
            ]
            if verified and len(verified) == len(pairs):
                passed_a = any(pair.variants["A"].functional_passed for pair in verified)
                passed_b = any(pair.variants["B"].functional_passed for pair in verified)
                basis = "external_grader"
            elif all(
                pair.variants[label].verification_status == "agent_status_only"
                for pair in pairs for label in ("A", "B")
            ):
                passed_a = any(pair.variants["A"].agent_reported_success for pair in pairs)
                passed_b = any(pair.variants["B"].agent_reported_success for pair in pairs)
                basis = "agent_reported_success"
            else:
                passed_a = passed_b = False
                basis = "excluded"

            if basis == "excluded":
                outcome = "excluded"
            elif passed_b and not passed_a:
                outcome = "improved"
            elif passed_a and not passed_b:
                outcome = "regressed"
            elif passed_a:
                outcome = "both_passed"
            else:
                outcome = "both_failed"
            category = pairs[0].category
            self.task_comparisons.append({
                "task_id": task_id,
                "category": category,
                "outcome": outcome,
                "basis": basis,
                "valid_attempt_pairs": len(verified) if basis == "external_grader" else len(pairs),
                "attempt_pairs": len(pairs),
            })
            self.task_outcome_counts[outcome] = self.task_outcome_counts.get(outcome, 0) + 1
            category_bucket = self.task_outcomes_by_category.setdefault(category, {})
            category_bucket[outcome] = category_bucket.get(outcome, 0) + 1

    @property
    def bootstrap_ci95(self) -> tuple[float, float] | None:
        return self.bootstrap_ci

    @property
    def comparisons(self) -> dict[str, MetricComparison]:
        return self.metric_results

    def to_dict(self) -> dict[str, Any]:
        """Return one JSON-safe source of truth for CLI and HTTP reports."""
        payload = redact_secrets(_json_safe({"schema_version": 1, **asdict(self)}))
        payload["reproducibility"] = sanitize_value(payload.get("reproducibility", {}))
        for observation in payload.get("task_observations", []):
            variants = observation.get("variants", {})
            if not isinstance(variants, dict):
                continue
            for variant in variants.values():
                if not isinstance(variant, dict):
                    continue
                if variant.get("grade_reason"):
                    variant["grade_reason"] = redact_text(variant["grade_reason"])
                if variant.get("error"):
                    variant["error"] = redact_text(variant["error"])
                variant["grade_details"] = sanitize_value(variant.get("grade_details", {}))
                variant["run_score"] = sanitize_value(variant.get("run_score"))
        return payload

    def write_json(self, path: str | Path) -> Path:
        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                self.to_dict(), ensure_ascii=False, indent=2, allow_nan=False,
            ),
            encoding="utf-8",
        )
        return target

    def write_html(self, path: str | Path) -> Path:
        """Persist a self-contained dashboard for the paired experiment."""
        from synapse.eval.visualize import render_experiment_html

        return render_experiment_html(self.to_dict(), path)


@dataclass
class Experiment:
    """Compare two configurations with paired, randomized per-round execution.

    A legacy benchmark may accept ``benchmark(config)`` and return a float. A
    benchmark may also return ``dict[str, float]`` and optionally accept a
    second ``seed`` argument; both variants in a round receive the same seed.
    It may return ``(metrics, run_score)`` with a trusted ``comparability``
    envelope. ``workspace_factory(label, task_id, attempt)`` supplies distinct
    workspace paths plus a shared A/B ``baseline_id`` for formal comparison.
    A ``Benchmark`` or ``BenchmarkTask`` uses ``task_runner(config, task, seed)``
    and records the external grader result for every paired attempt. Runners may
    accept a fourth ``attempt`` argument to scope stateful sequence instances.
    """

    id: str
    name: str
    variables: dict[str, Any]
    agent_config_a: dict[str, Any]
    agent_config_b: dict[str, Any]
    benchmark: Any
    effective_config_a: dict[str, Any] | None = None
    effective_config_b: dict[str, Any] | None = None
    runs_per_config: int = 6
    primary_metric: str | None = None
    direction: Direction = "lower"
    higher_is_better: bool | None = None
    metric_directions: dict[str, Direction] = field(default_factory=dict)
    guardrail_metrics: tuple[str, ...] = ()
    inferential_metrics: tuple[str, ...] | None = None
    diagnostic_metrics: tuple[str, ...] = ()
    binary_metrics: tuple[str, ...] = ()
    seed: int = 0
    confidence_level: float = 0.95
    bootstrap_samples: int = 5000
    randomization_samples: int = 10000
    alpha: float = 0.05
    task_runner: Any | None = field(default=None, repr=False)
    grade_task: Any | None = field(default=None, repr=False)
    allowed_config_diff_paths: tuple[str, ...] | None = None
    workspace_factory: Any | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.direction not in {"higher", "lower"}:
            raise ValueError("direction must be 'higher' or 'lower'")
        if self.higher_is_better is None:
            self.higher_is_better = self.direction == "higher"
        else:
            self.direction = "higher" if self.higher_is_better else "lower"
        if isinstance(self.runs_per_config, bool) or not isinstance(self.runs_per_config, int):
            raise ValueError("runs_per_config must be a positive integer")
        if self.runs_per_config < 1:
            raise ValueError("runs_per_config must be a positive integer")
        if (self.effective_config_a is None) != (self.effective_config_b is None):
            raise ValueError("effective configs must be supplied for both A and B")
        if self.allowed_config_diff_paths is not None:
            config_a = self.effective_config_a or self.agent_config_a
            config_b = self.effective_config_b or self.agent_config_b
            observed = set(_config_diff_paths(config_a, config_b))
            allowed = set(self.allowed_config_diff_paths)
            unexpected = sorted(observed - allowed)
            if unexpected:
                raise ValueError(
                    f"experiment configs differ outside allowed paths: {unexpected}"
                )

    def _base_comparability(self) -> tuple[list[str], dict[str, Any]]:
        config_a = self.effective_config_a or self.agent_config_a
        config_b = self.effective_config_b or self.agent_config_b
        diff_paths = _config_diff_paths(config_a, config_b)
        protected = _protected_config_diff_paths(diff_paths)
        issues: list[str] = []
        if self.allowed_config_diff_paths is None:
            issues.append("allowed_config_diff_paths_missing")
        issues.extend(f"protected_config_diff:{path}" for path in protected)
        if self.workspace_factory is None:
            issues.append("workspace_isolation_unproven")
        return issues, {
            "contract_version": 1,
            "required_runtime_evidence": [
                "model_id", "budgets", "permissions",
            ],
            "workspace_policy": (
                "distinct_lease_same_baseline_per_task_or_sequence_attempt"
            ),
            "allowed_config_diff_paths": (
                list(self.allowed_config_diff_paths)
                if self.allowed_config_diff_paths is not None else None
            ),
            "effective_config_diff_paths": diff_paths,
            "protected_config_diff_paths": protected,
            "workspace_factory": self.workspace_factory is not None,
            "workspace_instances": 0,
        }

    async def _variant_configs(
        self, attempt: int, task_id: str,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, str | None]]]:
        configs = {
            "A": dict(self.agent_config_a),
            "B": dict(self.agent_config_b),
        }
        if self.workspace_factory is None:
            return configs, {}
        workspaces: dict[str, dict[str, str | None]] = {}
        for label in ("A", "B"):
            value = self.workspace_factory(
                label=label, task_id=task_id, attempt=attempt,
            )
            if inspect.isawaitable(value):
                value = await value
            baseline_id = None
            if isinstance(value, Mapping):
                baseline = value.get("baseline_id")
                baseline_id = str(baseline).strip() if baseline is not None else None
                value = value.get("path")
            try:
                workspace = Path(value).expanduser().resolve()
            except TypeError as exc:
                raise TypeError(
                    "workspace_factory must return a path or path/baseline mapping"
                ) from exc
            if not workspace.is_dir():
                raise ValueError("workspace_factory must return an existing directory")
            workspaces[label] = {
                "path": str(workspace),
                "baseline_id": baseline_id,
            }
            configs[label]["workspace_root"] = str(workspace)
        return configs, workspaces

    @staticmethod
    def _workspace_comparability(
        workspaces: dict[str, dict[str, str | None]],
        seen_paths: set[str],
    ) -> tuple[list[str], set[str]]:
        if not workspaces:
            return [], set()
        issues: list[str] = []
        paths = [str(workspaces[label]["path"]) for label in ("A", "B")]
        baselines = [workspaces[label]["baseline_id"] for label in ("A", "B")]
        if len(set(paths)) != 2 or any(path in seen_paths for path in paths):
            issues.append("workspace_factory_reused_path")
        seen_paths.update(paths)
        if any(not baseline for baseline in baselines):
            issues.append("workspace_baseline_missing")
        elif len(set(baselines)) != 1:
            issues.append("workspace_baseline_mismatch")
        return issues, {
            _evidence_fingerprint(baseline) for baseline in baselines if baseline
        }

    @staticmethod
    def _runtime_comparability(
        facts: dict[str, list[dict[str, Any]]],
    ) -> tuple[list[str], dict[str, Any]]:
        issues: list[str] = []
        evidence: dict[str, Any] = {
            "runtime_observations": {
                label: len(facts[label]) for label in ("A", "B")
            },
            "runtime_evidence_sources": {
                label: sorted({
                    str(item.get("source", "")) for item in facts[label]
                    if item.get("source")
                })
                for label in ("A", "B")
            },
        }

        if any(
            item.get("source") not in _TRUSTED_COMPARABILITY_SOURCES
            for label in ("A", "B") for item in facts[label]
            if item
        ):
            issues.append("runtime_evidence_untrusted")

        model_ids = {
            label: [
                str(item["model_id"]) for item in facts[label]
                if item.get("model_id")
            ]
            for label in ("A", "B")
        }
        evidence["actual_model_ids"] = {
            label: sorted(set(model_ids[label])) for label in ("A", "B")
        }
        if any(len(model_ids[label]) != len(facts[label]) for label in ("A", "B")):
            issues.append("actual_model_id_missing")
        if len(set(model_ids["A"] + model_ids["B"])) > 1:
            issues.append("actual_model_id_mismatch")

        for field, missing_issue, mismatch_issue in (
            ("budgets", "budget_evidence_missing", "budget_mismatch"),
            ("permissions", "permission_evidence_missing", "permission_mismatch"),
        ):
            fingerprints = {
                label: [
                    _evidence_fingerprint(item[field]) for item in facts[label]
                    if field in item
                ]
                for label in ("A", "B")
            }
            evidence[f"{field}_fingerprints"] = {
                label: sorted(set(fingerprints[label])) for label in ("A", "B")
            }
            if any(
                len(fingerprints[label]) != len(facts[label])
                for label in ("A", "B")
            ):
                issues.append(missing_issue)
            if len(set(fingerprints["A"] + fingerprints["B"])) > 1:
                issues.append(mismatch_issue)
        return issues, evidence

    async def _run_once(self, config: dict[str, Any], seed: int, style: str | None) -> Any:
        config_copy = dict(config)
        if style == "keyword":
            value = self.benchmark(config_copy, seed=seed)
        elif style == "positional":
            value = self.benchmark(config_copy, seed)
        else:
            value = self.benchmark(config_copy)
        return await value if inspect.isawaitable(value) else value

    async def _run_callback_experiment(self) -> ExperimentResult:
        rng = random.Random(self.seed)
        run_order: list[str] = []
        round_seeds: list[int] = []
        collected = {"A": {}, "B": {}}
        runtime_facts: dict[str, list[dict[str, Any]]] = {"A": [], "B": []}
        comparability_issues, comparability_evidence = self._base_comparability()
        seen_workspaces: set[str] = set()
        workspace_baselines: set[str] = set()
        metric_names: set[str] | None = None
        primary_metric = self.primary_metric
        seed_style = _seed_call_style(self.benchmark)

        for attempt in range(1, self.runs_per_config + 1):
            round_seed = rng.getrandbits(63)
            round_seeds.append(round_seed)
            order = ["A", "B"]
            if rng.getrandbits(1):
                order.reverse()
            attempt_configs, workspaces = await self._variant_configs(
                attempt, "callback",
            )
            workspace_issues, baselines = self._workspace_comparability(
                workspaces, seen_workspaces,
            )
            comparability_issues.extend(workspace_issues)
            workspace_baselines.update(baselines)

            for label in order:
                raw = await self._run_once(
                    attempt_configs[label], round_seed, seed_style,
                )
                run_score = None
                if (
                    isinstance(raw, tuple)
                    and len(raw) == 2
                    and isinstance(raw[1], Mapping)
                ):
                    raw, run_score = raw
                runtime_facts[label].append(
                    _runtime_comparability_evidence(run_score)
                )
                scalar_name = primary_metric or "metric"
                metrics = _normalize_output(raw, scalar_name)
                if primary_metric is None:
                    primary_metric = next(iter(metrics))
                if primary_metric not in metrics:
                    raise ValueError(f"primary metric '{primary_metric}' was not returned")
                if metric_names is None:
                    metric_names = set(metrics)
                    collected = {
                        "A": {name: [] for name in metrics},
                        "B": {name: [] for name in metrics},
                    }
                elif set(metrics) != metric_names:
                    raise ValueError("benchmark must return the same metrics for every run")
                for name, value in metrics.items():
                    collected[label][name].append(value)
                run_order.append(label)

        primary_metric = primary_metric or "metric"
        if metric_names is None:
            collected = {"A": {primary_metric: []}, "B": {primary_metric: []}}
        runtime_issues, runtime_evidence = self._runtime_comparability(runtime_facts)
        comparability_issues.extend(runtime_issues)
        comparability_evidence.update(runtime_evidence)
        comparability_evidence["workspace_instances"] = len(seen_workspaces)
        comparability_evidence["workspace_baseline_fingerprints"] = sorted(
            workspace_baselines
        )

        if (
            self.inferential_metrics is not None
            and "agent_reported_success" in self.inferential_metrics
        ):
            raise ValueError(
                "agent_reported_success cannot be confirmatory without an external grader"
            )
        guardrail_metrics = tuple(
            name for name in self.guardrail_metrics
            if name != "agent_reported_success"
        )
        inferential_metrics = self.inferential_metrics
        if inferential_metrics is None:
            inferential_metrics = tuple(dict.fromkeys(
                name for name in (primary_metric, *guardrail_metrics)
                if name != "agent_reported_success"
            ))
        diagnostic_metrics = tuple(dict.fromkeys((
            *self.diagnostic_metrics,
            *(
                ("agent_reported_success",)
                if "agent_reported_success" in collected["A"] else ()
            ),
        )))

        from synapse.eval.runner import _fingerprint, _sanitize_config

        config_a = _sanitize_config(self.agent_config_a)
        config_b = _sanitize_config(self.agent_config_b)
        effective_config_a = _sanitize_config(self.effective_config_a or self.agent_config_a)
        effective_config_b = _sanitize_config(self.effective_config_b or self.agent_config_b)
        return ExperimentResult(
            experiment_id=self.id,
            experiment_name=self.name,
            variables=dict(self.variables),
            config_a=config_a,
            config_b=config_b,
            config_fingerprints={"A": _fingerprint(config_a), "B": _fingerprint(config_b)},
            effective_config_a=effective_config_a,
            effective_config_b=effective_config_b,
            effective_config_fingerprints={
                "A": _fingerprint(effective_config_a),
                "B": _fingerprint(effective_config_b),
            },
            config_diff_paths=_config_diff_paths(effective_config_a, effective_config_b),
            allowed_config_diff_paths=self.allowed_config_diff_paths,
            metrics_a=collected["A"][primary_metric],
            metrics_b=collected["B"][primary_metric],
            primary_metric=primary_metric,
            direction=self.direction,
            higher_is_better=self.higher_is_better,
            metric_directions=dict(self.metric_directions),
            guardrail_metrics=guardrail_metrics,
            inferential_metrics=inferential_metrics,
            diagnostic_metrics=diagnostic_metrics,
            all_metrics_a=collected["A"],
            all_metrics_b=collected["B"],
            comparability_eligible=not comparability_issues,
            comparability_issues=list(dict.fromkeys(comparability_issues)),
            comparability_evidence=comparability_evidence,
            seed=self.seed,
            confidence_level=self.confidence_level,
            bootstrap_samples=self.bootstrap_samples,
            randomization_samples=self.randomization_samples,
            alpha=self.alpha,
            run_order=run_order,
            round_seeds=round_seeds,
        )

    async def _run_benchmark_variant(
        self,
        benchmark: Any,
        task: Any,
        config: dict[str, Any],
        label: str,
        seed: int,
        attempt: int,
        attempt_style: str | None,
    ) -> VariantObservation:
        from synapse.eval.runner import Benchmark, BenchmarkRunner, _find_runtime_score

        execution: Any = None

        async def execute(full_task: Any) -> Any:
            nonlocal execution
            if attempt_style == "keyword":
                value = self.task_runner(
                    dict(config), full_task, seed, attempt=attempt,
                )
            elif attempt_style == "positional":
                value = self.task_runner(dict(config), full_task, seed, attempt)
            else:
                value = self.task_runner(dict(config), full_task, seed)
            execution = await value if inspect.isawaitable(value) else value
            return execution

        async def unused(_description: str) -> Any:
            raise AssertionError("task_runner must handle benchmark tasks")

        single = Benchmark(
            name=benchmark.name,
            tasks=[task],
            grader=benchmark.grader,
            metadata=dict(benchmark.metadata),
        )
        report = await BenchmarkRunner().run(
            single,
            unused,
            grade_task=self.grade_task,
            task_runner=execute,
        )
        task_result = report.results[0]
        agent_result = execution[0] if isinstance(execution, tuple) else execution
        status = task_result.status
        metrics = {
            "duration_ms": float(task_result.duration_ms),
            "agent_reported_success": float(status == "success"),
        }

        agent_metrics = getattr(agent_result, "metrics", None)
        runtime = _find_runtime_score(task_result.run_score)
        efficiency = runtime.get("efficiency", {}) if runtime else {}

        def number(name: str, fallback: Any = 0) -> float:
            value = efficiency.get(name, fallback)
            return _as_float(value, name) if value is not None else 0.0

        token_source = str(efficiency.get("token_count_source", "")).strip()
        agent_tokens_input = getattr(agent_metrics, "tokens_input", 0)
        agent_tokens_output = getattr(agent_metrics, "tokens_output", 0)
        has_agent_token_counts = bool(agent_tokens_input or agent_tokens_output)
        has_token_counts = token_source != "unavailable" and bool(
            token_source
            or "tokens_input" in efficiency
            or "tokens_output" in efficiency
            or has_agent_token_counts
        )
        if has_token_counts:
            tokens_input = number(
                "tokens_input", agent_tokens_input
            )
            tokens_output = number(
                "tokens_output", agent_tokens_output
            )
            metrics.update({
                "tokens_input": tokens_input,
                "tokens_output": tokens_output,
                "tokens": tokens_input + tokens_output,
            })
        if agent_metrics is not None or efficiency:
            tool_calls = number(
                "tool_call_count", getattr(agent_metrics, "tool_call_count", 0)
            )
            tool_successes = number(
                "tool_success_count", getattr(agent_metrics, "tool_success_count", 0)
            )
            metrics.update({
                "tool_calls": tool_calls,
                "tool_success_rate": (
                    number("tool_success_rate")
                    if "tool_success_rate" in efficiency
                    else tool_successes / tool_calls if tool_calls else 0.0
                ),
            })
        if "cost_estimate_usd" in efficiency:
            metrics["cost_estimate_usd"] = number("cost_estimate_usd")

        safety = runtime.get("safety") if runtime else None
        if isinstance(safety, Mapping):
            metrics.update({
                "safety_risk_attempts": _mapping_number(
                    safety, "injection_attempts"
                ) + _mapping_number(safety, "dangerous_command_attempts"),
                "safety_policy_blocks": _mapping_number(safety, "auth_blocks"),
                "safety_violations": _mapping_number(
                    safety, "sandbox_violations"
                ) + _mapping_number(safety, "out_of_workspace_access"),
            })

        verified = task_result.verification_status == "verified"
        if verified:
            metrics["functional_success"] = float(task_result.passed)
            metrics["grader_score"] = float(task_result.score)
        return VariantObservation(
            label=label,
            metrics=metrics,
            agent_status=status,
            agent_reported_success=status == "success",
            functional_passed=task_result.passed if verified else None,
            grader_score=float(task_result.score) if verified else None,
            verification_status=task_result.verification_status,
            grade_reason=task_result.grade_reason,
            grade_details=_json_safe(task_result.grade_details),
            run_score=_json_safe(task_result.run_score),
            error=task_result.error,
        )

    async def _run_task_experiment(self, source: Any) -> ExperimentResult:
        from synapse.eval.runner import (
            Benchmark,
            BenchmarkTask,
            _find_runtime_score,
            _fingerprint,
            _reproducibility_metadata,
            _sanitize_config,
        )

        if self.task_runner is None:
            raise ValueError("task_runner is required for Benchmark experiments")
        benchmark = source if isinstance(source, Benchmark) else Benchmark(
            name=self.name,
            tasks=[source],
            grader=self.grade_task,
        )
        if not benchmark.tasks or not all(
            isinstance(task, BenchmarkTask) for task in benchmark.tasks
        ):
            raise TypeError("benchmark tasks must be BenchmarkTask instances")
        task_ids = [task.id for task in benchmark.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("benchmark task ids must be unique")

        rng = random.Random(self.seed)
        run_order: list[str] = []
        round_seeds: list[int] = []
        task_observations: list[TaskPairObservation] = []
        metric_pairs: dict[str, list[tuple[str, int, float, float]]] = {}
        excluded_pairs: list[dict[str, Any]] = []
        comparability_issues, comparability_evidence = self._base_comparability()
        seen_workspaces: set[str] = set()
        workspace_baselines: set[str] = set()
        has_external_grader = self.grade_task is not None or benchmark.grader is not None
        attempt_style = _attempt_call_style(self.task_runner)

        for attempt in range(1, self.runs_per_config + 1):
            workspace_groups: dict[
                str, tuple[
                    dict[str, dict[str, Any]],
                    dict[str, dict[str, str | None]],
                ]
            ] = {}
            for task in benchmark.tasks:
                workspace_group = str(
                    task.metadata.get("sequence_id") or task.id
                )
                if workspace_group not in workspace_groups:
                    workspace_groups[workspace_group] = await self._variant_configs(
                        attempt, workspace_group,
                    )
                    _, workspaces = workspace_groups[workspace_group]
                    workspace_issues, baselines = self._workspace_comparability(
                        workspaces, seen_workspaces,
                    )
                    comparability_issues.extend(workspace_issues)
                    workspace_baselines.update(baselines)
                attempt_configs, _ = workspace_groups[workspace_group]
                round_seed = rng.getrandbits(63)
                round_seeds.append(round_seed)
                order = ["A", "B"]
                if rng.getrandbits(1):
                    order.reverse()
                variants: dict[str, VariantObservation] = {}
                for label in order:
                    variants[label] = await self._run_benchmark_variant(
                        benchmark, task, attempt_configs[label], label,
                        round_seed, attempt, attempt_style,
                    )
                    run_order.append(label)
                task_observations.append(TaskPairObservation(
                    task_id=task.id,
                    attempt=attempt,
                    seed=round_seed,
                    run_order=(order[0], order[1]),
                    variants=variants,
                    category=str(task.metadata.get("category", "uncategorized")),
                ))
                if has_external_grader and any(
                    variants[label].verification_status != "verified"
                    for label in ("A", "B")
                ):
                    excluded_pairs.append({
                        "task_id": task.id,
                        "attempt": attempt,
                        "reason": "incomplete_verified_pair",
                        "verification_status": {
                            label: variants[label].verification_status
                            for label in ("A", "B")
                        },
                    })
                for name in variants["A"].metrics.keys() & variants["B"].metrics.keys():
                    metric_pairs.setdefault(name, []).append((
                        task.id,
                        attempt,
                        variants["A"].metrics[name],
                        variants["B"].metrics[name],
                    ))

        collected: dict[str, dict[str, list[float]]] = {"A": {}, "B": {}}
        metric_task_ids: dict[str, list[str]] = {}
        metric_coverage: dict[str, dict[str, int]] = {}
        scheduled_task_count = len(benchmark.tasks)
        for name, pairs in metric_pairs.items():
            counts: dict[str, int] = {}
            for task_id, _attempt, _value_a, _value_b in pairs:
                counts[task_id] = counts.get(task_id, 0) + 1
            complete_tasks = {
                task_id for task_id, count in counts.items()
                if count == self.runs_per_config
            }
            complete_pairs = [pair for pair in pairs if pair[0] in complete_tasks]
            collected["A"][name] = [pair[2] for pair in complete_pairs]
            collected["B"][name] = [pair[3] for pair in complete_pairs]
            metric_task_ids[name] = [pair[0] for pair in complete_pairs]
            metric_coverage[name] = {
                "scheduled_tasks": scheduled_task_count,
                "complete_tasks": len(complete_tasks),
                "excluded_tasks": scheduled_task_count - len(complete_tasks),
                "scheduled_attempt_pairs": scheduled_task_count * self.runs_per_config,
                "complete_attempt_pairs": len(complete_pairs),
            }

        primary_metric = self.primary_metric or (
            "functional_success" if has_external_grader else "agent_reported_success"
        )
        collected["A"].setdefault(primary_metric, [])
        collected["B"].setdefault(primary_metric, [])
        metric_task_ids.setdefault(primary_metric, [])
        metric_coverage.setdefault(primary_metric, {
            "scheduled_tasks": scheduled_task_count,
            "complete_tasks": 0,
            "excluded_tasks": scheduled_task_count,
            "scheduled_attempt_pairs": scheduled_task_count * self.runs_per_config,
            "complete_attempt_pairs": 0,
        })
        default_directions: dict[str, Direction] = {
            "functional_success": "higher",
            "grader_score": "higher",
            "agent_reported_success": "higher",
            "duration_ms": "lower",
            "tokens_input": "lower",
            "tokens_output": "lower",
            "tokens": "lower",
            "tool_calls": "lower",
            "tool_success_rate": "higher",
            "cost_estimate_usd": "lower",
            "safety_risk_attempts": "lower",
            "safety_policy_blocks": "lower",
            "safety_violations": "lower",
        }
        metric_directions = {
            **{name: default_directions.get(name, self.direction) for name in collected["A"]},
            **self.metric_directions,
        }
        direction = (
            default_directions.get(primary_metric, self.direction)
            if self.primary_metric is None
            else self.direction
        )
        config_a = _sanitize_config(self.agent_config_a)
        config_b = _sanitize_config(self.agent_config_b)
        effective_config_a = _sanitize_config(self.effective_config_a or self.agent_config_a)
        effective_config_b = _sanitize_config(self.effective_config_b or self.agent_config_b)
        effective_fingerprints = {
            "A": _fingerprint(effective_config_a),
            "B": _fingerprint(effective_config_b),
        }
        if has_external_grader:
            inferential_metrics = tuple(dict.fromkeys(
                self.inferential_metrics
                or (primary_metric, *self.guardrail_metrics)
            ))
        else:
            inferential_metrics = tuple(dict.fromkeys(
                self.inferential_metrics
                if self.inferential_metrics is not None
                else self.guardrail_metrics
            ))
            if "agent_reported_success" in inferential_metrics:
                raise ValueError(
                    "agent_reported_success cannot be confirmatory without an external grader"
                )
        confirmatory = set(inferential_metrics)
        auto_diagnostic = {
            "agent_reported_success",
            "duration_ms",
            "tokens_input",
            "tokens_output",
            "tokens",
            "tool_calls",
            "tool_success_rate",
            "cost_estimate_usd",
            "safety_risk_attempts",
            "safety_policy_blocks",
            "safety_violations",
        }
        diagnostic_metrics = tuple(sorted(
            (set(self.diagnostic_metrics) | (auto_diagnostic & set(collected["A"])))
            - confirmatory
        ))
        reproducibility = _reproducibility_metadata(
            benchmark, None,
            self.grade_task if self.grade_task is not None else benchmark.grader,
        )

        def runtime_ids(label: str, field: str) -> list[str]:
            values = set()
            for pair in task_observations:
                runtime = _find_runtime_score(pair.variants[label].run_score)
                value = str(runtime.get(field, "")).strip() if runtime else ""
                if value:
                    values.add(value)
            return sorted(values)

        reproducibility.update({
            "effective_config_fingerprints": effective_fingerprints,
            "actual_model_ids": {
                label: runtime_ids(label, "model_id") for label in ("A", "B")
            },
            "actual_run_ids": {
                label: runtime_ids(label, "run_id") for label in ("A", "B")
            },
            "experiment_seed": self.seed,
            "runs_per_config": self.runs_per_config,
        })
        runtime_facts = {
            label: [
                _runtime_comparability_evidence(pair.variants[label].run_score)
                for pair in task_observations
            ]
            for label in ("A", "B")
        }
        runtime_issues, runtime_evidence = self._runtime_comparability(runtime_facts)
        comparability_issues.extend(runtime_issues)
        comparability_evidence.update(runtime_evidence)
        comparability_evidence["workspace_instances"] = len(seen_workspaces)
        comparability_evidence["workspace_baseline_fingerprints"] = sorted(
            workspace_baselines
        )
        return ExperimentResult(
            experiment_id=self.id,
            experiment_name=self.name,
            variables=dict(self.variables),
            config_a=config_a,
            config_b=config_b,
            config_fingerprints={"A": _fingerprint(config_a), "B": _fingerprint(config_b)},
            effective_config_a=effective_config_a,
            effective_config_b=effective_config_b,
            effective_config_fingerprints=effective_fingerprints,
            config_diff_paths=_config_diff_paths(effective_config_a, effective_config_b),
            allowed_config_diff_paths=self.allowed_config_diff_paths,
            metrics_a=collected["A"][primary_metric],
            metrics_b=collected["B"][primary_metric],
            primary_metric=primary_metric,
            direction=direction,
            higher_is_better=direction == "higher",
            metric_directions=metric_directions,
            guardrail_metrics=tuple(self.guardrail_metrics),
            inferential_metrics=inferential_metrics,
            diagnostic_metrics=diagnostic_metrics,
            all_metrics_a=collected["A"],
            all_metrics_b=collected["B"],
            metric_task_ids=metric_task_ids,
            binary_metrics=tuple(
                name for name in dict.fromkeys(("functional_success", *self.binary_metrics))
                if name in collected["A"]
            ),
            task_observations=task_observations,
            excluded_pairs=excluded_pairs,
            metric_coverage=metric_coverage,
            reproducibility=reproducibility,
            comparability_eligible=not comparability_issues,
            comparability_issues=list(dict.fromkeys(comparability_issues)),
            comparability_evidence=comparability_evidence,
            seed=self.seed,
            confidence_level=self.confidence_level,
            bootstrap_samples=self.bootstrap_samples,
            randomization_samples=self.randomization_samples,
            alpha=self.alpha,
            run_order=run_order,
            round_seeds=round_seeds,
        )

    async def run(self) -> ExperimentResult:
        """Run paired attempts, using task clusters for Benchmark inputs."""
        from synapse.eval.runner import Benchmark, BenchmarkTask

        if isinstance(self.benchmark, (Benchmark, BenchmarkTask)):
            return await self._run_task_experiment(self.benchmark)
        return await self._run_callback_experiment()
