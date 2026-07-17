"""Built-in benchmarks for Synapse evaluation.

- ``SWEBenchAdapter``: SWE-bench adapter with anti-contamination measures.
- ``ProcessQualityBenchmark``: Self-built benchmark for process quality.
"""

from synapse.eval.benchmarks.swebench import SWEBenchAdapter
from synapse.eval.benchmarks.process_bench import ProcessQualityBenchmark

__all__ = ["SWEBenchAdapter", "ProcessQualityBenchmark"]
