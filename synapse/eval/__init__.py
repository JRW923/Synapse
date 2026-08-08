"""Synapse eval — evaluation metrics, benchmarks, and experiment framework."""

from synapse.eval.runner import (
    BenchmarkRunner,
    Benchmark,
    BenchmarkTask,
    TaskGrade,
    TaskResult,
    BenchmarkResult,
)
from synapse.eval.benchmarks import (
    SWEBenchAdapter, ProcessQualityBenchmark, RepoPytestBenchmark,
)

__all__ = [
    "BenchmarkRunner",
    "Benchmark",
    "BenchmarkTask",
    "TaskGrade",
    "TaskResult",
    "BenchmarkResult",
    "SWEBenchAdapter",
    "RepoPytestBenchmark",
    "ProcessQualityBenchmark",
]
