"""A/B Experiment Framework.

Provides ``Experiment`` and ``ExperimentResult`` for comparing two
agent configurations (A and B) by running each against a common
benchmark and computing statistical significance via scipy.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from scipy import stats


# ---------------------------------------------------------------------------
# ExperimentResult
# ---------------------------------------------------------------------------


@dataclass
class ExperimentResult:
    """Holds the outcome of an A/B experiment.

    On creation, ``p_value`` is computed automatically via
    ``scipy.stats.ttest_ind`` (two-sided independent t-test) comparing
    *metrics_a* and *metrics_b*.  The ``p_value`` may be ``None`` if
    the t-test cannot be performed (e.g. fewer than 2 data points).
    """

    experiment_id: str = ""
    experiment_name: str = ""
    metrics_a: list[float] = field(default_factory=list)
    metrics_b: list[float] = field(default_factory=list)

    p_value: float | None = field(init=False)
    winner: str | None = field(init=False)  # "A", "B", or None

    def __post_init__(self) -> None:
        if len(self.metrics_a) >= 2 and len(self.metrics_b) >= 2:
            result = stats.ttest_ind(self.metrics_a, self.metrics_b)
            self.p_value = float(result.pvalue)
        else:
            self.p_value = None

        # Determine winner (lower metric = better)
        if self.p_value is not None and self.p_value < 0.05:
            mean_a = sum(self.metrics_a) / len(self.metrics_a) if self.metrics_a else 0
            mean_b = sum(self.metrics_b) / len(self.metrics_b) if self.metrics_b else 0
            if mean_a < mean_b:
                self.winner = "A"
            elif mean_b < mean_a:
                self.winner = "B"
            else:
                self.winner = None
        else:
            self.winner = None


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------


@dataclass
class Experiment:
    """Defines an A/B experiment comparing two agent configurations.

    Parameters
    ----------
    id:
        Unique experiment identifier.
    name:
        Human-readable experiment name.
    variables:
        Free-form dict describing which variables differ between A and B.
    agent_config_a:
        Configuration for the control / variant-A agent.
    agent_config_b:
        Configuration for the treatment / variant-B agent.
    benchmark:
        An async callable ``benchmark(config: dict) -> float`` that
        runs a single trial and returns a metric (lower = better).
    runs_per_config:
        Number of independent trials per configuration (default 5).
    """

    id: str
    name: str
    variables: dict[str, Any]
    agent_config_a: dict[str, Any]
    agent_config_b: dict[str, Any]
    benchmark: Any  # async callable: (config: dict) -> float
    runs_per_config: int = 5

    async def run(self) -> ExperimentResult:
        """Execute the experiment and return an ``ExperimentResult``.

        Runs the benchmark ``runs_per_config`` times for config A,
        then ``runs_per_config`` times for config B, collecting
        the returned float metrics into separate lists.
        """
        metrics_a: list[float] = []
        metrics_b: list[float] = []

        # Run config A
        for _ in range(self.runs_per_config):
            score = await self.benchmark(self.agent_config_a)
            metrics_a.append(float(score))

        # Run config B
        for _ in range(self.runs_per_config):
            score = await self.benchmark(self.agent_config_b)
            metrics_b.append(float(score))

        return ExperimentResult(
            experiment_id=self.id,
            experiment_name=self.name,
            metrics_a=metrics_a,
            metrics_b=metrics_b,
        )
