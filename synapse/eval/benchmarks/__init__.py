"""Built-in benchmarks for Synapse evaluation.

- ``SWEBenchAdapter``: SWE-bench adapter with anti-contamination measures.
- ``ProcessQualityBenchmark``: Self-built benchmark for process quality.
"""

from synapse.eval.benchmarks.swebench import SWEBenchAdapter, SWEBenchExecution
from synapse.eval.benchmarks.process_bench import ProcessQualityBenchmark
from synapse.eval.benchmarks.repo_pytest import RepoPytestBenchmark, RepoPytestResult

__all__ = [
    "SWEBenchAdapter", "SWEBenchExecution", "ProcessQualityBenchmark",
    "RepoPytestBenchmark", "RepoPytestResult",
]
