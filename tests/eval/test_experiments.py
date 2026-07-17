"""Tests for A/B Experiment Framework — Experiment and ExperimentResult."""

import pytest

from synapse.eval.experiments import Experiment, ExperimentResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _CountingBenchmark:
    """Benchmark that returns a deterministic value and tracks invocations."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._counter = 0

    async def __call__(self, config: dict) -> float:
        label = config.get("label", "unknown")
        self._counter += 1
        self.calls.append(label)
        # Return slightly different values so metrics lists differ
        return float(self._counter)


@pytest.fixture
def counting_benchmark() -> _CountingBenchmark:
    return _CountingBenchmark()


@pytest.fixture
def simple_experiment(counting_benchmark: _CountingBenchmark) -> Experiment:
    return Experiment(
        id="exp-001",
        name="Test Experiment",
        variables={"temperature": "0.7 vs 1.0"},
        agent_config_a={"label": "A", "temperature": 0.7},
        agent_config_b={"label": "B", "temperature": 1.0},
        benchmark=counting_benchmark,
        runs_per_config=5,
    )


# ---------------------------------------------------------------------------
# Test 1: both configs run the correct number of times
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_both_configs_run(simple_experiment: Experiment, counting_benchmark: _CountingBenchmark) -> None:
    """Each config should be exercised exactly runs_per_config times."""
    result = await simple_experiment.run()

    # Metainfo preserved
    assert result.experiment_id == "exp-001"
    assert result.experiment_name == "Test Experiment"

    # Call counts
    calls_a = [c for c in counting_benchmark.calls if c == "A"]
    calls_b = [c for c in counting_benchmark.calls if c == "B"]
    assert len(calls_a) == 5
    assert len(calls_b) == 5
    assert len(counting_benchmark.calls) == 10

    # Metrics are collected separately
    assert len(result.metrics_a) == 5
    assert len(result.metrics_b) == 5
    assert result.metrics_a != result.metrics_b


# ---------------------------------------------------------------------------
# Test 2: p_value computed via scipy.stats.ttest_ind
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p_value_computed() -> None:
    """ExperimentResult should compute p_value and set winner when appropriate."""

    # Simulate distinct distributions — A is clearly better (lower = better)
    result = ExperimentResult(
        experiment_id="exp-002",
        experiment_name="p-value test",
        metrics_a=[1.0, 1.1, 0.9, 1.0, 1.1],   # mean ~1.0
        metrics_b=[5.0, 5.1, 4.9, 5.0, 5.1],   # mean ~5.0
    )

    # scipy.stats.ttest_ind computes two-sided t-test
    assert result.p_value is not None
    assert result.p_value < 0.05  # clearly significant for these values

    # Winner detection: lower metric wins → A
    assert result.winner == "A"


@pytest.mark.asyncio
async def test_no_winner_when_not_significant() -> None:
    """When p_value > 0.05, winner should be None."""
    result = ExperimentResult(
        experiment_id="exp-003",
        experiment_name="no-winner test",
        metrics_a=[1.0, 1.1, 0.9, 1.0, 1.1],
        metrics_b=[1.0, 1.1, 0.9, 1.0, 1.1],   # essentially identical
    )
    assert result.p_value is not None
    assert result.p_value > 0.05
    assert result.winner is None
