"""Reproducible evaluation governance primitives.

The module deliberately uses only the standard library. Formal benchmark
execution remains the responsibility of an official or otherwise trusted
runner; this module freezes and verifies the evidence around those runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any, Callable, Mapping, Sequence


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def _load_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    value = json.loads(text)
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("tasks"), list):
        return value["tasks"]
    raise ValueError("dataset must be a JSON list, JSON object with tasks, or JSONL")


def _task_id(task: Mapping[str, Any]) -> str:
    value = task.get("id", task.get("instance_id", task.get("task_id", "")))
    value = str(value).strip()
    if not value:
        raise ValueError("every dataset task must have id, instance_id, or task_id")
    return value


@dataclass(frozen=True)
class FrozenDatasetManifest:
    name: str
    version: str
    source: str
    license: str
    split: dict[str, list[str]]
    files: list[dict[str, Any]]
    tasks: list[dict[str, Any]]
    grader_version: str
    image_digest: str
    tombstones: list[dict[str, str]] = field(default_factory=list)
    schema_version: int = 1
    created_at: str = field(default_factory=_utc_now)
    manifest_sha256: str = ""

    def __post_init__(self) -> None:
        allowed = {"dev", "holdout", "formal"}
        unknown = set(self.split) - allowed
        if unknown:
            raise ValueError(f"unknown dataset splits: {sorted(unknown)}")
        assigned = [task_id for values in self.split.values() for task_id in values]
        if len(assigned) != len(set(assigned)):
            raise ValueError("a task cannot belong to multiple splits")
        task_ids = {str(task["id"]) for task in self.tasks}
        if set(assigned) != task_ids:
            raise ValueError("split membership must cover every task exactly once")
        tombstoned = {item["task_id"] for item in self.tombstones}
        if not tombstoned <= task_ids:
            raise ValueError("tombstones must reference tasks in this manifest")
        if not all((self.name, self.version, self.source, self.license,
                    self.grader_version, self.image_digest)):
            raise ValueError("dataset identity, grader version, and image digest are required")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["manifest_sha256"] = ""
        payload["manifest_sha256"] = _sha256_bytes(_canonical_json(payload))
        return payload

    def write(self, path: str | Path) -> Path:
        target = Path(path).expanduser().resolve()
        if target.exists():
            existing = json.loads(target.read_text(encoding="utf-8"))
            candidate = self.to_dict()
            if existing != candidate:
                raise FileExistsError(
                    "frozen dataset manifests are immutable; create a new version"
                )
            return target
        return _write_json_atomic(target, self.to_dict())

    def verify_dataset(self, dataset: str | Path) -> bool:
        path = Path(dataset).expanduser().resolve()
        expected = next(
            (item for item in self.files if item["name"] == path.name), None,
        )
        return bool(
            expected
            and path.is_file()
            and path.stat().st_size == int(expected["bytes"])
            and _sha256_file(path) == expected["sha256"]
        )

    @classmethod
    def read(cls, path: str | Path) -> "FrozenDatasetManifest":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        manifest_sha256 = payload.pop("manifest_sha256", "")
        candidate = dict(payload)
        candidate["manifest_sha256"] = ""
        if _sha256_bytes(_canonical_json(candidate)) != manifest_sha256:
            raise ValueError("dataset manifest integrity check failed")
        return cls(**payload, manifest_sha256=manifest_sha256)

    @classmethod
    def freeze(
        cls,
        dataset: str | Path,
        *,
        name: str,
        version: str,
        source: str,
        license: str,
        grader_version: str,
        image_digest: str,
        default_split: str = "dev",
        tombstones: Sequence[Mapping[str, str]] = (),
    ) -> "FrozenDatasetManifest":
        path = Path(dataset).expanduser().resolve()
        if default_split not in {"dev", "holdout", "formal"}:
            raise ValueError("default_split must be dev, holdout, or formal")
        records = _load_records(path)
        tasks: list[dict[str, Any]] = []
        split = {"dev": [], "holdout": [], "formal": []}
        seen: set[str] = set()
        for record in records:
            task_id = _task_id(record)
            if task_id in seen:
                raise ValueError(f"duplicate task id: {task_id}")
            seen.add(task_id)
            task_split = str(record.get("split", default_split))
            if task_split not in split:
                raise ValueError(f"invalid split for {task_id}: {task_split}")
            repository_id = str(record.get("repository_id", record.get("repo", "unknown")))
            tasks.append({
                "id": task_id,
                "split": task_split,
                "repository_id": repository_id,
                "sha256": _sha256_bytes(_canonical_json(record)),
            })
            split[task_split].append(task_id)
        raw = path.read_bytes()
        return cls(
            name=name,
            version=version,
            source=source,
            license=license,
            split=split,
            files=[{"name": path.name, "bytes": len(raw), "sha256": _sha256_bytes(raw)}],
            tasks=tasks,
            grader_version=grader_version,
            image_digest=image_digest,
            tombstones=[dict(item) for item in tombstones],
        )


@dataclass(frozen=True)
class ExperimentPreregistration:
    experiment_id: str
    primary_metric: str
    direction: str
    mde: float
    alpha: float
    power: float
    baseline_rate: float
    task_count: int
    repeat: int
    guardrails: dict[str, dict[str, Any]]
    stopping_rules: list[str]
    model_harness_matrix: list[dict[str, str]]
    dataset_manifest_sha256: str
    schema_version: int = 1
    created_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if self.direction not in {"higher", "lower"}:
            raise ValueError("direction must be higher or lower")
        if not 0 < self.mde < 1 or not 0 < self.alpha < 1 or not 0 < self.power < 1:
            raise ValueError("mde, alpha, and power must be between 0 and 1")
        if not 0 <= self.baseline_rate <= 1:
            raise ValueError("baseline_rate must be between 0 and 1")
        if self.task_count < self.minimum_task_count():
            raise ValueError(
                f"task_count {self.task_count} is below estimated minimum "
                f"{self.minimum_task_count()}"
            )
        if self.repeat < 1:
            raise ValueError("repeat must be positive")
        if not self.guardrails or not self.stopping_rules or not self.model_harness_matrix:
            raise ValueError("guardrails, stopping_rules, and model_harness_matrix are required")

    def minimum_task_count(self) -> int:
        z_alpha = NormalDist().inv_cdf(1 - self.alpha / 2)
        z_power = NormalDist().inv_cdf(self.power)
        candidate = min(1.0, max(0.0, self.baseline_rate + self.mde))
        variance = (
            self.baseline_rate * (1 - self.baseline_rate)
            + candidate * (1 - candidate)
        )
        return max(2, math.ceil(((z_alpha + z_power) ** 2 * variance) / self.mde**2))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["estimated_minimum_task_count"] = self.minimum_task_count()
        payload["preregistration_sha256"] = _sha256_bytes(_canonical_json(payload))
        return payload

    def write(self, path: str | Path) -> Path:
        target = Path(path).expanduser().resolve()
        if target.exists():
            raise FileExistsError("preregistrations are immutable")
        return _write_json_atomic(target, self.to_dict())


@dataclass(frozen=True)
class GraderCalibration:
    total: int
    graded_expected_passes: int
    graded_expected_failures: int
    false_accepts: int
    false_rejects: int
    grader_errors: int
    mutation_coverage: dict[str, int]
    disagreements: list[dict[str, Any]]

    @property
    def false_accept_rate(self) -> float | None:
        return (
            self.false_accepts / self.graded_expected_failures
            if self.graded_expected_failures else None
        )

    @property
    def false_reject_rate(self) -> float | None:
        return (
            self.false_rejects / self.graded_expected_passes
            if self.graded_expected_passes else None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "false_accept_rate": self.false_accept_rate,
            "false_reject_rate": self.false_reject_rate,
        }

    @classmethod
    def run(
        cls,
        cases: Sequence[Mapping[str, Any]],
        grader: Callable[[Mapping[str, Any]], bool],
    ) -> "GraderCalibration":
        false_accepts = false_rejects = grader_errors = 0
        graded_expected_passes = graded_expected_failures = 0
        mutation_coverage: dict[str, int] = {}
        disagreements: list[dict[str, Any]] = []
        for case in cases:
            expected = bool(case["expected_pass"])
            mutation = str(case.get("mutation", "none"))
            mutation_coverage[mutation] = mutation_coverage.get(mutation, 0) + 1
            try:
                actual = bool(grader(case))
            except Exception as exc:
                grader_errors += 1
                disagreements.append({
                    "id": str(case.get("id", "unknown")),
                    "expected_pass": expected,
                    "actual_pass": None,
                    "error_type": type(exc).__name__,
                })
                continue
            graded_expected_passes += int(expected)
            graded_expected_failures += int(not expected)
            if actual != expected:
                false_accepts += int(actual and not expected)
                false_rejects += int(expected and not actual)
                disagreements.append({
                    "id": str(case.get("id", "unknown")),
                    "expected_pass": expected,
                    "actual_pass": actual,
                })
        return cls(
            total=len(cases),
            graded_expected_passes=graded_expected_passes,
            graded_expected_failures=graded_expected_failures,
            false_accepts=false_accepts,
            false_rejects=false_rejects, grader_errors=grader_errors,
            mutation_coverage=mutation_coverage, disagreements=disagreements,
        )


class ArtifactStore:
    """Local content-addressed store; disabled unless explicitly constructed."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def put(self, content: bytes | str | Mapping[str, Any], *, media_type: str = "") -> dict[str, Any]:
        if isinstance(content, str):
            raw = content.encode("utf-8")
            media_type = media_type or "text/plain; charset=utf-8"
        elif isinstance(content, Mapping):
            raw = _canonical_json(content)
            media_type = media_type or "application/json"
        else:
            raw = bytes(content)
            media_type = media_type or "application/octet-stream"
        digest = _sha256_bytes(raw)
        target = self.root / "sha256" / digest[:2] / digest
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and _sha256_file(target) != digest:
            raise RuntimeError("artifact digest collision or store corruption")
        if not target.exists():
            descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{digest}.")
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(raw)
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return {
            "uri": f"artifact://sha256/{digest}",
            "sha256": digest,
            "bytes": len(raw),
            "media_type": media_type,
        }

    def verify(self, reference: Mapping[str, Any]) -> bool:
        digest = str(reference["sha256"])
        target = self.root / "sha256" / digest[:2] / digest
        return target.is_file() and target.stat().st_size == int(reference["bytes"]) \
            and _sha256_file(target) == digest


class RunRegistry:
    """Append-only registry backed by one immutable JSON file per report ID."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.runs = self.root / "runs"
        self.reports = self.root / "reports" / "sha256"

    def register(
        self,
        report: str | Path,
        *,
        report_id: str,
        series: str,
        role: str,
        status: str,
        approval: str,
        preregistration_sha256: str = "",
        baseline_report_id: str = "",
    ) -> dict[str, Any]:
        if role not in {"baseline", "candidate"}:
            raise ValueError("role must be baseline or candidate")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", report_id):
            raise ValueError("report_id must be a portable identifier")
        target = self.runs / f"{report_id}.json"
        if target.exists():
            raise FileExistsError(f"report_id already registered: {report_id}")
        if role == "candidate":
            baselines = {
                item["report_id"]: item for item in self.records(series=series)
                if item.get("role") == "baseline"
            }
            if baseline_report_id not in baselines:
                raise ValueError("candidate must reference a registered baseline in the same series")
        report_path = Path(report).expanduser().resolve()
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        report_sha256 = _sha256_file(report_path)
        archived_report = self.reports / report_sha256[:2] / f"{report_sha256}.json"
        if not archived_report.exists():
            archived_report.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                dir=archived_report.parent, prefix=f".{report_sha256}.",
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(report_path.read_bytes())
                os.replace(temporary, archived_report)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        reproducibility = payload.get("reproducibility", {})
        manifest = reproducibility.get("dataset_manifest", {})
        record = {
            "schema_version": 1,
            "report_id": report_id,
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "series": series,
            "role": role,
            "baseline_report_id": baseline_report_id,
            "status": status,
            "approval": approval,
            "report": {
                "name": report_path.name,
                "uri": f"registry://sha256/{report_sha256}",
                "sha256": report_sha256,
                "bytes": report_path.stat().st_size,
            },
            "preregistration_sha256": preregistration_sha256,
            "fingerprints": {
                "dataset": manifest.get("manifest_sha256", manifest.get("taskset_sha256", "")),
                "grader": manifest.get("grader_sha256", ""),
                "image": manifest.get("image_digest", ""),
                "code": reproducibility.get("git_commit", ""),
                "config": reproducibility.get("config_fingerprint", ""),
            },
            "metrics": {
                key: payload.get(key) for key in (
                    "task_success_rate", "false_success_rate", "p95_duration_ms",
                    "safety_violation_rate", "infrastructure_failure_rate", "p95_tokens",
                )
            },
        }
        _write_json_atomic(target, record)
        return record

    def records(self, *, series: str | None = None) -> list[dict[str, Any]]:
        if not self.runs.exists():
            return []
        records = [json.loads(path.read_text(encoding="utf-8")) for path in self.runs.glob("*.json")]
        records.sort(key=lambda item: item["registered_at"])
        return [item for item in records if series is None or item["series"] == series]

    def verify(self, reports: str | Path | None = None) -> list[dict[str, Any]]:
        report_dir = Path(reports).expanduser().resolve() if reports is not None else None
        results = []
        for record in self.records():
            digest = record["report"]["sha256"]
            path = (
                report_dir / record["report"]["name"] if report_dir is not None
                else self.reports / digest[:2] / f"{digest}.json"
            )
            valid = path.is_file() and path.stat().st_size == record["report"]["bytes"] \
                and _sha256_file(path) == record["report"]["sha256"]
            results.append({"report_id": record["report_id"], "valid": valid})
        return results


class TrendAnalyzer:
    DEFAULT_THRESHOLDS = {
        "task_success_rate": -0.05,
        "false_success_rate": 0.02,
        "safety_violation_rate": 0.0,
        "p95_duration_ms": 0.20,
        "p95_tokens": 0.20,
        "infrastructure_failure_rate": 0.02,
    }

    @classmethod
    def analyze(
        cls,
        records: Sequence[Mapping[str, Any]],
        *,
        thresholds: Mapping[str, float] | None = None,
    ) -> dict[str, Any]:
        thresholds = {**cls.DEFAULT_THRESHOLDS, **(thresholds or {})}
        if not records:
            return {"comparable": False, "issues": ["no runs"], "points": [], "alerts": []}
        series = {str(record.get("series", "")) for record in records}
        if len(series) != 1:
            return {
                "comparable": False,
                "issues": ["runs belong to different series"],
                "points": [], "alerts": [],
            }
        fingerprint_issues = []
        for name in ("dataset", "grader", "image"):
            values = {
                str(record.get("fingerprints", {}).get(name, ""))
                for record in records
            }
            if len(values) > 1:
                fingerprint_issues.append(f"{name} fingerprint changed within series")
        if fingerprint_issues:
            return {
                "comparable": False, "issues": fingerprint_issues,
                "points": [], "alerts": [],
            }
        baseline = next((record for record in records if record.get("role") == "baseline"), records[0])
        baseline_metrics = cls._metrics(baseline)
        alerts: list[dict[str, Any]] = []
        points = []
        for record in records:
            metrics = cls._metrics(record)
            deltas = {}
            for name, value in metrics.items():
                base = baseline_metrics.get(name)
                if value is None or base is None:
                    continue
                delta = value - base
                relative = delta / abs(base) if base else delta
                deltas[name] = relative
                threshold = thresholds.get(name)
                if threshold is not None and (
                    (threshold < 0 and relative < threshold)
                    or (threshold >= 0 and relative > threshold)
                ):
                    alerts.append({
                        "report_id": record["report_id"], "metric": name,
                        "relative_change": round(relative, 6), "threshold": threshold,
                    })
            points.append({
                "report_id": record["report_id"],
                "registered_at": record["registered_at"],
                "metrics": metrics,
                "relative_to_baseline": deltas,
            })
        return {"comparable": True, "issues": [], "points": points, "alerts": alerts}

    @staticmethod
    def _metrics(record: Mapping[str, Any]) -> dict[str, float | None]:
        metrics = dict(record.get("metrics", {}))
        return {
            "task_success_rate": metrics.get("task_success_rate"),
            "false_success_rate": metrics.get("false_success_rate"),
            "safety_violation_rate": metrics.get("safety_violation_rate"),
            "p95_duration_ms": metrics.get("p95_duration_ms"),
            "p95_tokens": metrics.get("p95_tokens"),
            "infrastructure_failure_rate": metrics.get("infrastructure_failure_rate"),
        }


def _main() -> int:
    parser = argparse.ArgumentParser(description="Synapse evaluation governance")
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("dataset")
    freeze.add_argument("output")
    for name in ("name", "version", "source", "license", "grader-version", "image-digest"):
        freeze.add_argument(f"--{name}", required=True)
    freeze.add_argument("--default-split", choices=("dev", "holdout", "formal"), default="dev")
    verify = subparsers.add_parser("verify")
    verify.add_argument("registry")
    verify.add_argument("reports", nargs="?")
    verify_manifest = subparsers.add_parser("verify-manifest")
    verify_manifest.add_argument("manifest")
    verify_manifest.add_argument("dataset")
    trend = subparsers.add_parser("trend")
    trend.add_argument("registry")
    trend.add_argument("--series", required=True)
    preregister = subparsers.add_parser("preregister")
    preregister.add_argument("spec", help="JSON specification")
    preregister.add_argument("output")
    calibrate = subparsers.add_parser("calibrate")
    calibrate.add_argument("cases", help="JSON cases with expected_pass and actual_pass")
    calibrate.add_argument("output")
    register = subparsers.add_parser("register")
    register.add_argument("registry")
    register.add_argument("report")
    register.add_argument("--report-id", required=True)
    register.add_argument("--series", required=True)
    register.add_argument("--role", choices=("baseline", "candidate"), required=True)
    register.add_argument("--status", required=True)
    register.add_argument("--approval", required=True)
    register.add_argument("--baseline-report-id", default="")
    register.add_argument("--preregistration-sha256", default="")
    args = parser.parse_args()
    if args.command == "freeze":
        manifest = FrozenDatasetManifest.freeze(
            args.dataset, name=args.name, version=args.version, source=args.source,
            license=args.license, grader_version=args.grader_version,
            image_digest=args.image_digest, default_split=args.default_split,
        )
        manifest.write(args.output)
        print(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2))
    elif args.command == "preregister":
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        preregistration = ExperimentPreregistration(**spec)
        preregistration.write(args.output)
        print(json.dumps(preregistration.to_dict(), ensure_ascii=False, indent=2))
    elif args.command == "calibrate":
        cases = _load_records(Path(args.cases))
        result = GraderCalibration.run(cases, lambda case: bool(case["actual_pass"]))
        _write_json_atomic(Path(args.output).expanduser().resolve(), result.to_dict())
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    elif args.command == "register":
        record = RunRegistry(args.registry).register(
            args.report, report_id=args.report_id, series=args.series,
            role=args.role, status=args.status, approval=args.approval,
            preregistration_sha256=args.preregistration_sha256,
            baseline_report_id=args.baseline_report_id,
        )
        print(json.dumps(record, ensure_ascii=False, indent=2))
    elif args.command == "verify-manifest":
        valid = FrozenDatasetManifest.read(args.manifest).verify_dataset(args.dataset)
        print(json.dumps({"valid": valid}))
        return 0 if valid else 1
    elif args.command == "verify":
        print(json.dumps(RunRegistry(args.registry).verify(args.reports), indent=2))
    else:
        registry = RunRegistry(args.registry)
        print(json.dumps(TrendAnalyzer.analyze(registry.records(series=args.series)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
