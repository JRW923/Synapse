"""Benchmark Runner — executes benchmark tasks against Synapse Agent.

Provides the data model (BenchmarkTask, TaskResult, BenchmarkResult, Benchmark)
and BenchmarkRunner that orchestrates execution and metric aggregation.

This module lives in ``eval/`` and only consumes ``core/`` and ``protocols/``.
It never imports from ``modules/`` directly.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import time
import inspect
import json
import math
import platform
import random
import subprocess
from collections.abc import Callable, Awaitable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from synapse.protocols.planner import AgentResult, ResultStatus
from synapse.eval.reporting import (
    is_sensitive_key,
    redact_secrets,
    redact_text,
    sanitize_value,
    text_fingerprint,
)


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
        score = float(self.score)
        if not math.isfinite(score):
            raise ValueError("grader score must be finite")
        self.score = max(0.0, min(1.0, score))


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
    base_task_id: str = ""
    attempt: int = 1
    output: str = ""
    duration_ms: int = 0
    error: str | None = None
    passed: bool = False
    score: float = 0.0
    category: str = "uncategorized"
    grade_reason: str = ""
    grade_details: dict[str, Any] = field(default_factory=dict)
    verification_status: str = "not_graded"
    # Fatal LLM-side failure bucket from ExecutionMetrics.llm_failure
    # ("provider_unavailable" | "auth" | "llm_error" | ""). Non-empty means the
    # attempt never got a fair shot at the task.
    failure_kind: str = ""
    run_score: dict[str, Any] | None = None
    artifact_ref: dict[str, Any] | None = None


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
    pass_rate_ci95: list[float] = field(default_factory=lambda: [0.0, 0.0])
    mean_score_ci95: list[float] = field(default_factory=lambda: [0.0, 0.0])
    pass_at_k: float = 0.0
    tokens_input: int = 0
    tokens_output: int = 0
    total_cost_usd: float = 0.0
    tool_success_rate: float = 0.0
    schema_version: int = 2
    attempt_total: int = 0
    scored_attempt_total: int = 0
    excluded_attempts: int = 0
    attempt_passed: int = 0
    attempt_pass_rate: float = 0.0
    attempt_pass_rate_ci95: list[float] = field(default_factory=lambda: [0.0, 0.0])
    task_total: int = 0
    scored_task_total: int = 0
    task_succeeded: int = 0
    task_success_rate: float = 0.0
    task_success_rate_ci95: list[float] = field(default_factory=lambda: [0.0, 0.0])
    task_success_k: int = 1
    pass_at_k_by_k: dict[str, float] = field(default_factory=dict)
    pass_at_k_ci95_by_k: dict[str, list[float]] = field(default_factory=dict)
    pass_power_k_by_k: dict[str, float] = field(default_factory=dict)
    pass_power_k_ci95_by_k: dict[str, list[float]] = field(default_factory=dict)
    agent_reported_successes: int = 0
    verified_agent_reported_successes: int = 0
    false_successes: int = 0
    false_success_rate: float | None = None
    unverified_attempts: int = 0
    grader_error_attempts: int = 0
    median_duration_ms: float = 0.0
    p95_duration_ms: float = 0.0
    tokens_per_passed_attempt: float | None = None
    cost_per_passed_attempt_usd: float | None = None
    tokens_per_succeeded_task: float | None = None
    cost_per_succeeded_task_usd: float | None = None
    efficiency_provenance: dict[str, Any] = field(default_factory=dict)
    reproducibility: dict[str, Any] = field(default_factory=dict)
    inference_cluster: str = "task"
    repository_cluster_count: int = 0
    safety_violation_attempts: int = 0
    safety_violation_rate: float = 0.0
    infrastructure_failure_attempts: int = 0
    infrastructure_failure_rate: float = 0.0
    p95_tokens: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe report without persisting free-form task content."""
        tasks = []
        for result in self.results:
            item = {
                "task_id": result.task_id,
                "base_task_id": result.base_task_id or result.task_id,
                "attempt": result.attempt,
                "status": result.status,
                "output": redact_text(result.output),
                "duration_ms": result.duration_ms,
                "error": redact_text(result.error) if result.error else None,
                "passed": result.passed,
                "score": result.score,
                "category": result.category,
                "grade_reason": redact_text(result.grade_reason)
                if result.grade_reason else "",
                "grade_details": sanitize_value(result.grade_details),
                "verification_status": result.verification_status,
                "failure_kind": result.failure_kind,
                "run_score": sanitize_value(result.run_score),
                "artifact_ref": result.artifact_ref,
            }
            tasks.append(item)
        payload = {
            "schema_version": self.schema_version,
            "name": self.name,
            "total": self.total,
            "completed": self.completed,
            "failed": self.failed,
            "passed": self.passed,
            "pass_rate": self.pass_rate,
            "mean_score": self.mean_score,
            "duration_ms": self.duration_ms,
            "started_at": self.started_at,
            "pass_rate_ci95": self.pass_rate_ci95,
            "mean_score_ci95": self.mean_score_ci95,
            "pass_at_k": self.pass_at_k,
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "total_cost_usd": self.total_cost_usd,
            "tool_success_rate": self.tool_success_rate,
            "attempt_total": self.attempt_total,
            "scored_attempt_total": self.scored_attempt_total,
            "excluded_attempts": self.excluded_attempts,
            "attempt_passed": self.attempt_passed,
            "attempt_pass_rate": self.attempt_pass_rate,
            "attempt_pass_rate_ci95": self.attempt_pass_rate_ci95,
            "task_total": self.task_total,
            "scored_task_total": self.scored_task_total,
            "task_succeeded": self.task_succeeded,
            "task_success_rate": self.task_success_rate,
            "task_success_rate_ci95": self.task_success_rate_ci95,
            "task_success_k": self.task_success_k,
            "pass_at_k_by_k": self.pass_at_k_by_k,
            "pass_at_k_ci95_by_k": self.pass_at_k_ci95_by_k,
            "pass_power_k_by_k": self.pass_power_k_by_k,
            "pass_power_k_ci95_by_k": self.pass_power_k_ci95_by_k,
            "agent_reported_successes": self.agent_reported_successes,
            "verified_agent_reported_successes": self.verified_agent_reported_successes,
            "false_successes": self.false_successes,
            "false_success_rate": self.false_success_rate,
            "unverified_attempts": self.unverified_attempts,
            "grader_error_attempts": self.grader_error_attempts,
            "median_duration_ms": self.median_duration_ms,
            "p95_duration_ms": self.p95_duration_ms,
            "tokens_per_passed_attempt": self.tokens_per_passed_attempt,
            "cost_per_passed_attempt_usd": self.cost_per_passed_attempt_usd,
            "tokens_per_succeeded_task": self.tokens_per_succeeded_task,
            "cost_per_succeeded_task_usd": self.cost_per_succeeded_task_usd,
            "efficiency_provenance": sanitize_value(self.efficiency_provenance),
            "reproducibility": sanitize_value(self.reproducibility),
            "inference_cluster": self.inference_cluster,
            "repository_cluster_count": self.repository_cluster_count,
            "safety_violation_attempts": self.safety_violation_attempts,
            "safety_violation_rate": self.safety_violation_rate,
            "infrastructure_failure_attempts": self.infrastructure_failure_attempts,
            "infrastructure_failure_rate": self.infrastructure_failure_rate,
            "p95_tokens": self.p95_tokens,
            "metadata": sanitize_value(self.metadata),
            "by_category": self.by_category,
            "results": tasks,
        }
        return redact_secrets(payload)

    def write_json(self, path: str | Path) -> Path:
        """Persist the report and return its resolved path."""
        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return target

    def write_html(self, path: str | Path) -> Path:
        """Persist a self-contained dashboard for the benchmark report."""
        from synapse.eval.visualize import render_html

        return render_html(self.to_dict(), path)

    def write_csv(self, path: str | Path) -> Path:
        """Persist flattened task metrics for spreadsheets/plotting."""
        from synapse.eval.visualize import write_csv

        return write_csv(self.to_dict(), path)


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


def _wilson_interval(successes: int, total: int) -> list[float]:
    """Return a conservative 95% Wilson interval for a pass proportion."""
    if total <= 0:
        return [0.0, 0.0]
    z = 1.96
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return [round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4)]


def _mean_interval(values: list[float]) -> list[float]:
    """Return a normal-approximation 95% interval for mean task score."""
    if not values:
        return [0.0, 0.0]
    mean = sum(values) / len(values)
    if len(values) == 1:
        return [0.0, 1.0]
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    margin = 1.96 * math.sqrt(variance / len(values))
    return [round(max(0.0, mean - margin), 4), round(min(1.0, mean + margin), 4)]


def _estimated_pass_at_k(attempts: int, successes: int, k: int) -> float:
    """Return the standard unbiased pass@k estimate for one task."""
    if attempts <= 0 or k <= 0 or k > attempts:
        raise ValueError("pass@k requires 1 <= k <= attempts")
    if successes <= 0:
        return 0.0
    failures = attempts - successes
    if failures < k:
        return 1.0
    return 1.0 - (math.comb(failures, k) / math.comb(attempts, k))


def _estimated_pass_power_k(attempts: int, successes: int, k: int) -> float:
    """Return the probability that all ``k`` sampled attempts pass."""
    if attempts <= 0 or k <= 0 or k > attempts:
        raise ValueError("pass^k requires 1 <= k <= attempts")
    if successes < k:
        return 0.0
    return math.comb(successes, k) / math.comb(attempts, k)


def _bootstrap_mean_interval(
    values: list[float], *, seed: int = 0, samples: int = 2000,
) -> list[float]:
    """Return a deterministic percentile-bootstrap 95% CI for a mean."""
    if not values:
        return [0.0, 0.0]
    if len(values) == 1:
        # A task-level bootstrap has no sampling information with one task.
        return [0.0, 1.0]
    rng = random.Random(seed)
    size = len(values)
    means = sorted(
        sum(values[rng.randrange(size)] for _ in range(size)) / size
        for _ in range(samples)
    )
    lower = means[int(0.025 * (samples - 1))]
    upper = means[int(0.975 * (samples - 1))]
    return [round(lower, 4), round(upper, 4)]


def _cluster_bootstrap_mean_interval(
    values: list[float], cluster_ids: list[str], *, seed: int, samples: int = 2000,
) -> list[float]:
    """Bootstrap repository means so large repositories do not dominate inference."""
    if len(values) != len(cluster_ids):
        raise ValueError("values and cluster_ids must have equal length")
    buckets: dict[str, list[float]] = {}
    for value, cluster_id in zip(values, cluster_ids):
        buckets.setdefault(cluster_id, []).append(value)
    cluster_means = [sum(items) / len(items) for items in buckets.values()]
    return _bootstrap_mean_interval(cluster_means, seed=seed, samples=samples)


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sanitize_config(value: Any) -> Any:
    """Remove secrets before a config is persisted or fingerprinted."""
    if isinstance(value, dict):
        return {
            str(key): "<redacted>" if is_sensitive_key(key)
            else _sanitize_config(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_config(item) for item in value]
    return value


def _runner_comparability_envelope(
    effective_config: dict[str, Any], run_score: dict[str, Any],
) -> dict[str, Any]:
    """Build trusted runtime evidence from the runner's effective state."""
    provider = effective_config.get("provider", {})
    planning = effective_config.get("planning", {})
    context = effective_config.get("context", {})
    security = effective_config.get("security", {})
    tools = effective_config.get("tools", {})
    runtime = effective_config.get("runtime", {})
    capabilities = run_score.get("capabilities", {})
    return {
        "comparability": {
            "source": "runner",
            "model_id": run_score.get("model_id"),
            "budgets": {
                "provider_max_tokens": provider.get("max_tokens"),
                "provider_timeout_seconds": provider.get("timeout_seconds"),
                "provider_max_retries": provider.get("max_retries"),
                "max_iterations": planning.get("max_iterations"),
                "max_tokens_per_task": planning.get("max_tokens_per_task"),
                "total_timeout_seconds": planning.get("total_timeout_seconds"),
                "max_tool_result_chars": planning.get("max_tool_result_chars"),
                "context_total_tokens": context.get("total_tokens"),
            },
            "permissions": {
                "enabled_tools": tools.get("enabled"),
                "allowlist_commands": tools.get("allowlist_commands"),
                "sandbox_enabled": security.get("sandbox_enabled"),
                "sandbox_mode": security.get("sandbox_mode"),
                "sandbox_backend": security.get("sandbox_backend"),
                "sandbox_network": security.get("sandbox_network"),
                "sandbox_docker_image": security.get("sandbox_docker_image"),
                "auth_confirmation": security.get("auth_confirmation"),
                "allowed_paths": security.get("allowed_paths"),
                "allow_external": security.get("allow_external"),
                "enable_external_tools": runtime.get("enable_external_tools"),
                "mcp_servers": runtime.get("mcp_servers"),
                "actual_capabilities": capabilities,
            },
        },
    }


def _git_source_state() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
            encoding="utf-8", errors="replace", check=True, timeout=5,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True,
            encoding="utf-8", errors="replace", check=True, timeout=5,
        ).stdout.strip())
        return {"git_commit": commit, "git_dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"git_commit": "", "git_dirty": None}


def _callable_name(value: Any) -> str:
    module = getattr(value, "__module__", "")
    qualname = getattr(value, "__qualname__", getattr(value, "__name__", ""))
    return ".".join(part for part in (module, qualname) if part) or type(value).__name__


def _stable_code_value(value: Any) -> Any:
    if inspect.iscode(value):
        return {
            "bytecode": value.co_code.hex(),
            "constants": [_stable_code_value(item) for item in value.co_consts],
            "names": list(value.co_names),
            "freevars": list(value.co_freevars),
        }
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_stable_code_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _stable_code_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    return {"type": f"{type(value).__module__}.{type(value).__qualname__}"}


def _callable_fingerprint(value: Any) -> str:
    target = value
    if isinstance(value, (staticmethod, classmethod)):
        target = value.__func__
    elif not (inspect.isfunction(value) or inspect.ismethod(value) or inspect.isclass(value)):
        target = getattr(type(value), "__call__", value)
    payload: dict[str, Any] = {"name": _callable_name(value)}
    try:
        payload["source_sha256"] = hashlib.sha256(
            inspect.getsource(target).encode("utf-8", errors="replace")
        ).hexdigest()
    except (OSError, TypeError):
        pass
    code = getattr(target, "__code__", None)
    if code is not None:
        payload["code"] = _stable_code_value(code)
        payload["defaults"] = _stable_code_value(getattr(target, "__defaults__", None))
        payload["kwdefaults"] = _stable_code_value(getattr(target, "__kwdefaults__", None))
        closure = getattr(target, "__closure__", None)
        if closure:
            payload["closure"] = [
                _stable_code_value(cell.cell_contents) for cell in closure
            ]
    return _fingerprint(payload)


def _callable_owner(value: Any) -> type[Any] | None:
    bound = getattr(value, "__self__", None)
    if bound is not None:
        return bound if inspect.isclass(bound) else type(bound)
    module = inspect.getmodule(value)
    qualname = getattr(value, "__qualname__", "")
    if module is None or "<locals>" in qualname:
        return None
    current: Any = module
    for part in qualname.split(".")[:-1]:
        current = getattr(current, part, None)
        if current is None:
            return None
    return current if inspect.isclass(current) else None


def _grader_components(grader: Callable[..., Any]) -> dict[str, str]:
    name = _callable_name(grader)
    components = {f"callable:{name}": _callable_fingerprint(grader)}
    owner = _callable_owner(grader)
    if owner is not None:
        owner_name = f"{owner.__module__}.{owner.__qualname__}"
        for member_name, member in vars(owner).items():
            target = member.__func__ if isinstance(member, (staticmethod, classmethod)) else member
            if inspect.isfunction(target) or inspect.ismethod(target):
                components[f"helper:{owner_name}.{member_name}"] = _callable_fingerprint(target)
    try:
        source_file = inspect.getsourcefile(grader)
    except TypeError:
        source_file = None
    if source_file:
        try:
            components[f"module:{getattr(grader, '__module__', '')}"] = hashlib.sha256(
                Path(source_file).read_bytes()
            ).hexdigest()
        except OSError:
            pass
    return dict(sorted(components.items()))


def _artifact_sha256s(value: Any) -> list[str]:
    digests: set[str] = set()

    def collect(item: Any) -> None:
        if isinstance(item, str):
            normalized = item.strip().lower()
            if normalized.startswith("sha256:"):
                normalized = normalized[7:]
            if len(normalized) == 64 and all(char in "0123456789abcdef" for char in normalized):
                digests.add(normalized)
        elif isinstance(item, dict):
            for nested in item.values():
                collect(nested)
        elif isinstance(item, (list, tuple, set)):
            for nested in item:
                collect(nested)

    collect(value)
    return sorted(digests)


def _command_summary(command: Any) -> dict[str, Any]:
    if isinstance(command, (list, tuple)):
        summary = {"type": "argv", "argv_count": len(command)}
        payload = _canonical_json(list(command))
    else:
        summary = {"type": "text"}
        payload = str(command)
    return {**summary, **text_fingerprint(payload)}


def _dataset_manifest(
    benchmark: Benchmark,
    sanitized_config: dict[str, Any],
    tasks: list[dict[str, Any]],
    grader_override: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Build a portable dataset/grader manifest from declared and task metadata."""
    declared = benchmark.metadata.get("dataset_manifest", {})
    if not isinstance(declared, dict):
        declared = {}
    evaluation = sanitized_config.get("evaluation", {})
    if not isinstance(evaluation, dict):
        evaluation = {}
    dataset_file = evaluation.get("dataset", {})
    if not isinstance(dataset_file, dict):
        dataset_file = {}
    if not dataset_file and isinstance(benchmark.metadata.get("dataset"), dict):
        dataset_file = dict(benchmark.metadata["dataset"])

    commands: dict[str, dict[str, Any]] = {}
    timeouts: set[float | int] = set()
    for task in benchmark.tasks:
        for key in ("grader_command", "test_command"):
            command = task.metadata.get(key)
            if command not in (None, "", []):
                commands[_canonical_json(command)] = _command_summary(command)
        timeout = task.metadata.get("timeout")
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
            timeouts.add(timeout)

    declared_commands = declared.get("grader_commands", [])
    if not isinstance(declared_commands, list):
        declared_commands = [declared_commands]
    for command in declared_commands:
        if command not in (None, "", []):
            commands[_canonical_json(command)] = _command_summary(command)
    declared_timeouts = declared.get("grader_timeouts_seconds", [])
    if not isinstance(declared_timeouts, list):
        declared_timeouts = [declared_timeouts]
    for timeout in declared_timeouts:
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
            timeouts.add(timeout)

    grader = grader_override if grader_override is not None else benchmark.grader
    grader_label = str(
        declared.get("grader")
        or benchmark.metadata.get("grader")
        or benchmark.metadata.get("functional_grader")
        or "agent_status_only"
    )
    grader_name = _callable_name(grader) if grader is not None else grader_label
    grader_version = str(
        declared.get("grader_version")
        or benchmark.metadata.get("grader_version")
        or getattr(grader, "__version__", "")
        or "unknown"
    )
    artifact_digests: set[str] = set()
    for key in (
        "grader_artifact_sha256", "grader_artifact_digest", "grader_artifacts",
    ):
        value = declared.get(key)
        if value is None:
            value = benchmark.metadata.get(key)
        artifact_digests.update(_artifact_sha256s(value))
    grader_components = _grader_components(grader) if grader is not None else {}
    grader_fingerprint = {
        "name": grader_name,
        "version": grader_version,
        "artifact_sha256": sorted(artifact_digests),
        "components": grader_components,
    }

    manifest = {
        "schema_version": 2,
        "name": str(declared.get("name") or benchmark.name),
        "version": str(
            evaluation.get("dataset_version")
            or declared.get("version")
            or benchmark.metadata.get("dataset_version")
            or "unknown"
        ),
        "source": str(
            evaluation.get("dataset_source")
            or declared.get("source")
            or benchmark.metadata.get("dataset_source")
            or "unknown"
        ),
        "license": str(
            evaluation.get("dataset_license")
            or declared.get("license")
            or benchmark.metadata.get("dataset_license")
            or "unknown"
        ),
        "task_count": len(tasks),
        "taskset_sha256": _fingerprint(tasks),
        "dataset_file": dataset_file,
        "selection": {"max_tasks": evaluation.get("max_tasks")},
        "grader": grader_name,
        "grader_label": grader_label,
        "grader_version": grader_version,
        "grader_artifact_sha256": sorted(artifact_digests),
        "grader_components": [
            {"name": name, "sha256": digest}
            for name, digest in grader_components.items()
        ],
        "grader_sha256": _fingerprint(grader_fingerprint),
        "grader_commands": [commands[key] for key in sorted(commands)],
        "grader_timeouts_seconds": sorted(timeouts),
        "split": declared.get("split", {}),
        "tombstones": declared.get("tombstones", []),
        "files": declared.get("files", []),
        "image_digest": str(declared.get("image_digest", "unknown")),
    }
    manifest["manifest_sha256"] = str(
        declared.get("manifest_sha256") or _fingerprint(manifest)
    )
    return manifest


def _reproducibility_metadata(
    benchmark: Benchmark,
    evaluation_config: dict[str, Any] | None,
    grader_override: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    tasks = [
        {
            "id": task.id,
            "description": task.description,
            "metadata": task.metadata,
            "expected_process_scores": task.expected_process_scores,
        }
        for task in benchmark.tasks
    ]
    sanitized_config = _sanitize_config(evaluation_config or {})
    try:
        version = importlib.metadata.version("synapse")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    return {
        "taskset_fingerprint": _fingerprint(tasks),
        "dataset_manifest": _dataset_manifest(
            benchmark, sanitized_config, tasks, grader_override,
        ),
        "config_fingerprint": _fingerprint(sanitized_config) if evaluation_config is not None else "",
        "effective_config": sanitized_config,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "synapse_version": version,
        **_git_source_state(),
    }


def _find_runtime_score(value: Any) -> dict[str, Any]:
    """Find the first nested runtime snapshot without coupling to a benchmark."""
    if not isinstance(value, dict):
        return {}
    if isinstance(value.get("efficiency"), dict):
        return value
    for key in ("runtime", "run_score"):
        nested = value.get(key)
        if isinstance(nested, dict):
            found = _find_runtime_score(nested)
            if found:
                return found
    for nested in value.values():
        if isinstance(nested, dict):
            found = _find_runtime_score(nested)
            if found:
                return found
    return {}


def _actual_model_ids(results: list[TaskResult]) -> list[str]:
    models = {
        str(runtime.get("model_id", "")).strip()
        for item in results
        if (runtime := _find_runtime_score(item.run_score))
    }
    return sorted(model for model in models if model)


def _actual_run_ids(results: list[TaskResult]) -> list[str]:
    run_ids = {
        str(runtime.get("run_id", "")).strip()
        for item in results
        if (runtime := _find_runtime_score(item.run_score))
    }
    return sorted(run_id for run_id in run_ids if run_id)


def _validate_run_inputs(benchmark: Benchmark, repeat: int) -> None:
    if isinstance(repeat, bool) or not isinstance(repeat, int) or repeat < 1:
        raise ValueError("repeat must be a positive integer")
    task_ids = [task.id for task in benchmark.tasks]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("benchmark task ids must be unique")


def build_benchmark_result(
    benchmark: Benchmark,
    results: list[TaskResult],
    *,
    duration_ms: int,
    started_at: str,
    repeat: int,
    metadata: dict[str, Any] | None = None,
    evaluation_config: dict[str, Any] | None = None,
    grader_override: Callable[..., Any] | None = None,
) -> BenchmarkResult:
    """Aggregate attempt results with explicit attempt- and task-level metrics."""
    _validate_run_inputs(benchmark, repeat)
    attempt_total = len(results)
    scored_results = [item for item in results if item.verification_status == "verified"]
    scored_attempt_total = len(scored_results)
    excluded_attempts = attempt_total - scored_attempt_total
    attempt_passed = sum(int(item.passed) for item in scored_results)
    completed = sum(item.status in {ResultStatus.SUCCESS.value, ResultStatus.PARTIAL.value} for item in results)
    failed = sum(item.status in {ResultStatus.FAILED.value, "error"} for item in results)

    category_buckets: dict[str, dict[str, float | int]] = {}
    for item in results:
        bucket = category_buckets.setdefault(
            item.category,
            {
                "total": 0, "scored": 0, "excluded": 0, "passed": 0,
                "pass_rate": 0.0, "mean_score": 0.0,
            },
        )
        bucket["total"] = int(bucket["total"]) + 1
        if item.verification_status == "verified":
            bucket["scored"] = int(bucket["scored"]) + 1
            bucket["passed"] = int(bucket["passed"]) + int(item.passed)
            bucket["mean_score"] = float(bucket["mean_score"]) + item.score
        else:
            bucket["excluded"] = int(bucket["excluded"]) + 1
    for bucket in category_buckets.values():
        count = int(bucket["scored"])
        bucket["pass_rate"] = round(int(bucket["passed"]) / count, 4) if count else 0.0
        bucket["mean_score"] = round(float(bucket["mean_score"]) / count, 4) if count else 0.0

    scores = [item.score for item in scored_results]
    attempt_pass_rate = (
        round(attempt_passed / scored_attempt_total, 4)
        if scored_attempt_total else 0.0
    )
    mean_score = (
        round(sum(scores) / scored_attempt_total, 4)
        if scored_attempt_total else 0.0
    )

    grouped: dict[str, list[bool]] = {task.id: [] for task in benchmark.tasks}
    for item in scored_results:
        base_id = item.base_task_id or item.task_id
        grouped.setdefault(base_id, []).append(item.passed)
    task_total = len(grouped)
    scored_group_items = [
        (task_id, outcomes) for task_id, outcomes in grouped.items()
        if len(outcomes) == repeat
    ]
    scored_groups = [outcomes for _task_id, outcomes in scored_group_items]
    scored_task_total = len(scored_groups)
    task_succeeded = sum(any(outcomes) for outcomes in scored_groups)
    task_success_rate = (
        round(task_succeeded / scored_task_total, 4) if scored_task_total else 0.0
    )

    task_repositories = {
        task.id: str(task.metadata.get("repository_id", "")).strip()
        for task in benchmark.tasks
    }
    use_repository_clusters = bool(scored_group_items) and all(
        task_repositories.get(task_id) for task_id, _outcomes in scored_group_items
    )
    repository_ids = [
        task_repositories[task_id] for task_id, _outcomes in scored_group_items
    ] if use_repository_clusters else []
    repository_cluster_count = len(set(repository_ids))
    inference_cluster = "repository" if use_repository_clusters else "task"
    task_success_values = [float(any(outcomes)) for outcomes in scored_groups]
    task_success_ci = (
        _cluster_bootstrap_mean_interval(
            task_success_values, repository_ids, seed=9_999,
        )
        if use_repository_clusters
        else _wilson_interval(task_succeeded, scored_task_total)
    )

    pass_at_k_by_k: dict[str, float] = {}
    pass_at_k_ci95_by_k: dict[str, list[float]] = {}
    pass_power_k_by_k: dict[str, float] = {}
    pass_power_k_ci95_by_k: dict[str, list[float]] = {}
    if scored_groups:
        for k in range(1, repeat + 1):
            task_estimates = [
                _estimated_pass_at_k(len(outcomes), sum(outcomes), k)
                for outcomes in scored_groups
            ]
            estimate = sum(task_estimates) / len(task_estimates)
            pass_at_k_by_k[str(k)] = round(estimate, 4)
            pass_at_k_ci95_by_k[str(k)] = (
                _cluster_bootstrap_mean_interval(
                    task_estimates, repository_ids, seed=10_000 + k,
                ) if use_repository_clusters else _bootstrap_mean_interval(
                    task_estimates, seed=10_000 + k,
                )
            )
            consistency_estimates = [
                _estimated_pass_power_k(len(outcomes), sum(outcomes), k)
                for outcomes in scored_groups
            ]
            consistency = sum(consistency_estimates) / len(consistency_estimates)
            pass_power_k_by_k[str(k)] = round(consistency, 4)
            pass_power_k_ci95_by_k[str(k)] = (
                _cluster_bootstrap_mean_interval(
                    consistency_estimates, repository_ids, seed=20_000 + k,
                ) if use_repository_clusters else _bootstrap_mean_interval(
                    consistency_estimates, seed=20_000 + k,
                )
            )

    tokens_input = 0
    tokens_output = 0
    total_cost = 0.0
    tool_calls = 0
    tool_successes = 0
    token_sources: set[str] = set()
    cost_rates: set[tuple[float, float]] = set()
    cost_is_estimate = False
    efficiency_attempts = 0
    token_count_attempts = 0
    per_attempt_tokens: list[float] = []
    safety_violation_attempts = 0
    for item in results:
        runtime = _find_runtime_score(item.run_score)
        safety = runtime.get("safety", {})
        if isinstance(safety, dict) and (
            int(safety.get("sandbox_violations", 0) or 0)
            + int(safety.get("out_of_workspace_access", 0) or 0)
        ) > 0:
            safety_violation_attempts += 1
        efficiency = runtime.get("efficiency", {})
        if not isinstance(efficiency, dict) or not efficiency:
            continue
        efficiency_attempts += 1
        tokens_input += int(efficiency.get("tokens_input", 0) or 0)
        tokens_output += int(efficiency.get("tokens_output", 0) or 0)
        total_cost += float(efficiency.get("cost_estimate_usd", 0) or 0)
        tool_calls += int(efficiency.get("tool_call_count", 0) or 0)
        tool_successes += int(efficiency.get("tool_success_count", 0) or 0)
        source = str(efficiency.get("token_count_source", "")).strip()
        if not source and (
            "tokens_input" in efficiency or "tokens_output" in efficiency
        ):
            source = "legacy_unlabeled"
        if source:
            token_sources.add(source)
        if source and source != "unavailable":
            token_count_attempts += 1
            per_attempt_tokens.append(float(
                int(efficiency.get("tokens_input", 0) or 0)
                + int(efficiency.get("tokens_output", 0) or 0)
            ))
        cost_is_estimate = cost_is_estimate or bool(efficiency.get("cost_is_estimate", False))
        input_rate = float(efficiency.get("input_cost_per_million_usd", 0) or 0)
        output_rate = float(efficiency.get("output_cost_per_million_usd", 0) or 0)
        if input_rate or output_rate:
            cost_rates.add((input_rate, output_rate))

    merged_metadata = {**benchmark.metadata, **(metadata or {}), "repeat": repeat}
    legacy_pass_at_k = pass_at_k_by_k.get(str(repeat), task_success_rate)
    agent_reported_successes = sum(
        item.status == ResultStatus.SUCCESS.value for item in results
    )
    false_successes = sum(
        item.status == ResultStatus.SUCCESS.value
        and item.verification_status == "verified"
        and not item.passed
        for item in results
    )
    verified_agent_reported_successes = sum(
        item.status == ResultStatus.SUCCESS.value
        and item.verification_status == "verified"
        for item in results
    )
    unverified_attempts = sum(
        item.verification_status != "verified" for item in results
    )
    grader_error_attempts = sum(
        item.verification_status == "grader_error" for item in results
    )
    reproducibility = _reproducibility_metadata(
        benchmark, evaluation_config, grader_override,
    )
    reproducibility["actual_model_ids"] = _actual_model_ids(results)
    reproducibility["actual_run_ids"] = _actual_run_ids(results)
    durations = [float(item.duration_ms) for item in results]
    total_tokens = tokens_input + tokens_output
    token_metrics_complete = attempt_total > 0 and token_count_attempts == attempt_total
    infrastructure_failure_attempts = sum(
        item.status == "error"
        or item.verification_status == "grader_error"
        or item.failure_kind == "provider_unavailable"
        for item in results
    )
    if efficiency_attempts < attempt_total:
        token_sources.add("missing")
    return BenchmarkResult(
        name=benchmark.name,
        total=attempt_total,
        completed=completed,
        failed=failed,
        results=results,
        duration_ms=duration_ms,
        passed=attempt_passed,
        pass_rate=attempt_pass_rate,
        mean_score=mean_score,
        by_category=category_buckets,
        metadata=merged_metadata,
        started_at=started_at,
        pass_rate_ci95=_wilson_interval(attempt_passed, scored_attempt_total),
        mean_score_ci95=_mean_interval(scores),
        pass_at_k=legacy_pass_at_k,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        total_cost_usd=round(total_cost, 6),
        tool_success_rate=round(tool_successes / tool_calls, 4) if tool_calls else 0.0,
        attempt_total=attempt_total,
        scored_attempt_total=scored_attempt_total,
        excluded_attempts=excluded_attempts,
        attempt_passed=attempt_passed,
        attempt_pass_rate=attempt_pass_rate,
        attempt_pass_rate_ci95=_wilson_interval(attempt_passed, scored_attempt_total),
        task_total=task_total,
        scored_task_total=scored_task_total,
        task_succeeded=task_succeeded,
        task_success_rate=task_success_rate,
        task_success_rate_ci95=task_success_ci,
        task_success_k=repeat,
        pass_at_k_by_k=pass_at_k_by_k,
        pass_at_k_ci95_by_k=pass_at_k_ci95_by_k,
        pass_power_k_by_k=pass_power_k_by_k,
        pass_power_k_ci95_by_k=pass_power_k_ci95_by_k,
        agent_reported_successes=agent_reported_successes,
        verified_agent_reported_successes=verified_agent_reported_successes,
        false_successes=false_successes,
        false_success_rate=round(false_successes / verified_agent_reported_successes, 4)
        if verified_agent_reported_successes else None,
        unverified_attempts=unverified_attempts,
        grader_error_attempts=grader_error_attempts,
        median_duration_ms=round(_percentile(durations, 0.5), 2),
        p95_duration_ms=round(_percentile(durations, 0.95), 2),
        tokens_per_passed_attempt=round(total_tokens / attempt_passed, 2)
        if token_metrics_complete and attempt_passed else None,
        cost_per_passed_attempt_usd=round(total_cost / attempt_passed, 6)
        if token_metrics_complete and attempt_passed else None,
        tokens_per_succeeded_task=round(total_tokens / task_succeeded, 2)
        if token_metrics_complete and task_succeeded else None,
        cost_per_succeeded_task_usd=round(total_cost / task_succeeded, 6)
        if token_metrics_complete and task_succeeded else None,
        efficiency_provenance={
            "token_count_sources": sorted(token_sources),
            "attempts_with_efficiency": efficiency_attempts,
            "attempts_with_token_counts": token_count_attempts,
            "attempt_total": attempt_total,
            "token_coverage": round(token_count_attempts / attempt_total, 4)
            if attempt_total else 0.0,
            "token_metrics_complete": token_metrics_complete,
            "cost_is_estimate": cost_is_estimate,
            "cost_rates_usd_per_million": [
                {"input": input_rate, "output": output_rate}
                for input_rate, output_rate in sorted(cost_rates)
            ],
        },
        reproducibility=reproducibility,
        inference_cluster=inference_cluster,
        repository_cluster_count=repository_cluster_count,
        safety_violation_attempts=safety_violation_attempts,
        safety_violation_rate=round(safety_violation_attempts / attempt_total, 4)
        if attempt_total else 0.0,
        infrastructure_failure_attempts=infrastructure_failure_attempts,
        infrastructure_failure_rate=round(
            infrastructure_failure_attempts / attempt_total, 4,
        ) if attempt_total else 0.0,
        p95_tokens=round(_percentile(per_attempt_tokens, 0.95), 2)
        if token_metrics_complete else None,
    )


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
        evaluation_config: dict[str, Any] | None = None,
        task_runner: Callable[[BenchmarkTask], Awaitable[Any]] | None = None,
        repeat: int = 1,
        artifact_store: Any | None = None,
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
            is used. Without either grader, Agent status is retained only as an
            unverified diagnostic and does not enter functional metrics.
        report_path:
            Optional JSON path for a bounded, machine-readable report.
        metadata:
            Free-form report metadata.
        evaluation_config:
            Effective evaluation configuration used for a secret-free
            reproducibility fingerprint.
        repeat:
            Number of independent attempts per task. Repeated task IDs use a
            ``#N`` suffix and the report also exposes aggregate pass@k.
        artifact_store:
            Optional content-addressed store. Only failed, errored, or grader-error
            attempts are archived; reports retain only URI/hash/size metadata.

        Returns
        -------
        BenchmarkResult
            Aggregated statistics and per-task results.
        """
        t0 = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat()
        results: list[TaskResult] = []
        _validate_run_inputs(benchmark, repeat)
        repeat_count = repeat
        for task in benchmark.tasks:
            for attempt in range(repeat_count):
                t_start = time.monotonic()
                task_id = task.id if repeat_count == 1 else f"{task.id}#{attempt + 1}"
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
                    task_result = TaskResult(
                        task_id=task_id,
                        status="error",
                        base_task_id=task.id,
                        attempt=attempt + 1,
                        output="",
                        duration_ms=int((time.monotonic() - t_start) * 1000),
                        error=str(exc),
                        category=str(task.metadata.get("category", "uncategorized")),
                    )
                    if artifact_store is not None:
                        task_result.artifact_ref = artifact_store.put({
                            "task_id": task_id,
                            "status": "error",
                            "error": str(exc),
                        })
                    results.append(task_result)
                    continue

                status = (
                    agent_result.status.value
                    if hasattr(agent_result.status, "value")
                    else str(agent_result.status)
                )
                duration_ms = int((time.monotonic() - t_start) * 1000)

                grader = grade_task or benchmark.grader
                verification_status = "agent_status_only"
                try:
                    if grader is None:
                        grade = TaskGrade(
                            passed=False,
                            score=0.0,
                            reason=(
                                "unverified agent-reported success"
                                if status == ResultStatus.SUCCESS.value
                                else f"unverified agent-reported {status}"
                            ),
                        )
                    else:
                        grade = grader(task, agent_result, run_score)
                        if inspect.isawaitable(grade):
                            grade = await grade
                        if not isinstance(grade, TaskGrade):
                            raise TypeError("grader must return TaskGrade")
                        verification_status = "verified"
                except Exception as exc:
                    grade = TaskGrade(False, 0.0, reason=f"grader error: {exc}")
                    verification_status = "grader_error"

                task_result = TaskResult(
                    task_id=task_id,
                    status=status,
                    base_task_id=task.id,
                    attempt=attempt + 1,
                    output=agent_result.output,
                    duration_ms=duration_ms,
                    passed=grade.passed,
                    score=grade.score,
                    category=str(task.metadata.get("category", "uncategorized")),
                    grade_reason=grade.reason,
                    grade_details=grade.details,
                    verification_status=verification_status,
                    failure_kind=str(getattr(
                        getattr(agent_result, "metrics", None), "llm_failure", "") or ""),
                    run_score=run_score,
                )
                if artifact_store is not None and (
                    not grade.passed
                    or status in {ResultStatus.FAILED.value, "error"}
                    or verification_status == "grader_error"
                ):
                    task_result.artifact_ref = artifact_store.put({
                        "task_id": task_id,
                        "status": status,
                        "output": agent_result.output,
                        "grade_reason": grade.reason,
                        "grade_details": grade.details,
                        "verification_status": verification_status,
                        "run_score": run_score,
                    })
                results.append(task_result)

        result = build_benchmark_result(
            benchmark,
            results,
            duration_ms=int((time.monotonic() - t0) * 1000),
            started_at=started_at,
            repeat=repeat_count,
            metadata=metadata,
            evaluation_config=evaluation_config,
            grader_override=grade_task if grade_task is not None else benchmark.grader,
        )
        if report_path is not None:
            result.write_json(report_path)
        return result
