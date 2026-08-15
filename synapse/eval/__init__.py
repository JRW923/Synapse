"""Synapse eval — evaluation metrics, benchmarks, and experiment framework."""

from synapse.eval.runner import (
    BenchmarkRunner,
    Benchmark,
    BenchmarkTask,
    TaskGrade,
    TaskResult,
    BenchmarkResult,
)
from synapse.eval.ablations import EvaluationAblations
from synapse.eval.experiments import Experiment, ExperimentResult
from synapse.eval.harness_adapter import CommandHarnessAdapter
from synapse.eval.benchmarks import (
    SWEBenchAdapter, ProcessQualityBenchmark, RepoPytestBenchmark,
    TerminalBenchAdapter, TerminalSmokeBenchmark,
)

__all__ = [
    "BenchmarkRunner",
    "Benchmark",
    "BenchmarkTask",
    "TaskGrade",
    "TaskResult",
    "BenchmarkResult",
    "EvaluationAblations",
    "Experiment",
    "ExperimentResult",
    "CommandHarnessAdapter",
    "SWEBenchAdapter",
    "RepoPytestBenchmark",
    "ProcessQualityBenchmark",
    "TerminalBenchAdapter",
    "TerminalSmokeBenchmark",
]
