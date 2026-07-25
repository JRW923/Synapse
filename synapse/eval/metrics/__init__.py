"""Evaluation metric collectors — EventBus subscribers that aggregate metrics."""

from dataclasses import dataclass, field, asdict

from synapse.eval.metrics.process import ProcessMetrics, ProcessSnapshot
from synapse.eval.metrics.quality import QualityMetrics, QualitySnapshot
from synapse.eval.metrics.efficiency import EfficiencyMetrics, EfficiencySnapshot
from synapse.eval.metrics.safety import SafetyMetrics, SafetySnapshot


@dataclass
class RunScore:
    """Aggregated runtime score for a single task run (TODO K).

    Combines the four EventBus-driven metric collectors so every task yields an
    observable process/quality/efficiency/safety score that can be reported or
    persisted to memory.
    """

    task: str = ""
    status: str = ""
    safety: SafetySnapshot = field(default_factory=SafetySnapshot)
    process: ProcessSnapshot = field(default_factory=ProcessSnapshot)
    quality: QualitySnapshot = field(default_factory=QualitySnapshot)
    efficiency: EfficiencySnapshot = field(default_factory=EfficiencySnapshot)

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "status": self.status,
            "safety": asdict(self.safety),
            "process": asdict(self.process),
            "quality": asdict(self.quality),
            "efficiency": asdict(self.efficiency),
        }


__all__ = [
    "ProcessMetrics",
    "ProcessSnapshot",
    "QualityMetrics",
    "QualitySnapshot",
    "EfficiencyMetrics",
    "EfficiencySnapshot",
    "SafetyMetrics",
    "SafetySnapshot",
    "RunScore",
]
