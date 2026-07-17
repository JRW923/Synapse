"""Tests for SWEBenchAdapter and ProcessQualityBenchmark."""

import pytest

from synapse.eval.runner import BenchmarkTask
from synapse.eval.benchmarks.swebench import SWEBenchAdapter
from synapse.eval.benchmarks.process_bench import ProcessQualityBenchmark


# ---------------------------------------------------------------------------
# Test 1: SWE-bench mutation changes task text
# ---------------------------------------------------------------------------

def test_swebench_mutation_changes_task_text() -> None:
    """mutate_task should produce a description that differs from the original."""
    task = BenchmarkTask(
        id="test-1",
        description="Fix the authentication bug in the login module",
    )
    mutated = SWEBenchAdapter.mutate_task(task, seed=42)

    # Description is changed — surface form differs
    assert mutated.description != task.description
    # ID and other metadata are preserved
    assert mutated.id == task.id

    # Deterministic: same seed → same result
    mutated2 = SWEBenchAdapter.mutate_task(task, seed=42)
    assert mutated2.description == mutated.description

    # Different seed → different result (probabilistic; overwhelmingly likely)
    mutated3 = SWEBenchAdapter.mutate_task(task, seed=99)
    assert mutated3.description != mutated.description


# ---------------------------------------------------------------------------
# Test 2: Process benchmark tasks are loadable
# ---------------------------------------------------------------------------

def test_process_bench_tasks_loadable() -> None:
    """All process-quality benchmark tasks should be well-formed."""
    tasks = ProcessQualityBenchmark.tasks()

    assert len(tasks) >= 4  # at least 4 task categories

    categories = set()
    for task in tasks:
        assert task.id, f"Task {task} has no id"
        assert task.description, f"Task {task.id} has no description"
        # Each task should have at least one expected process score
        assert isinstance(task.expected_process_scores, dict)
        # Track categories
        categories.add(task.id.split("-")[0])

    # Must cover all four process-quality dimensions
    assert "reuse" in categories
    assert "rootcause" in categories
    assert "testpersist" in categories
    assert "instruct" in categories
