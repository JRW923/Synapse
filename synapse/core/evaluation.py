"""Evaluation-only runtime switches shared by Core and adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class EvaluationAblations:
    """Harness modules kept enabled for an evaluation variant."""

    context: bool = True
    memory: bool = True
    completion_gate: bool = True
    action_auth: bool = True

    @classmethod
    def from_value(
        cls, value: "EvaluationAblations | Mapping[str, Any] | None",
    ) -> "EvaluationAblations":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("eval_ablation must be a mapping")
        unknown = set(value) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unknown eval ablation fields: {sorted(unknown)}")
        if any(not isinstance(item, bool) for item in value.values()):
            raise TypeError("eval ablation values must be boolean")
        return cls(**dict(value))

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)
