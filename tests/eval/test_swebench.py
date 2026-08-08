"""Tests for SWEBenchAdapter and ProcessQualityBenchmark."""

import pytest
import subprocess

from synapse.eval.runner import BenchmarkTask
from synapse.protocols.planner import AgentResult, ResultStatus
from synapse.eval.benchmarks.swebench import SWEBenchAdapter
from synapse.eval.benchmarks.process_bench import ProcessQualityBenchmark
from synapse.eval.benchmarks.repo_pytest import RepoPytestBenchmark


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


def test_process_benchmark_grader_uses_runtime_snapshot() -> None:
    task = ProcessQualityBenchmark.tasks()[0]
    result = AgentResult(status=ResultStatus.SUCCESS, output="ok")
    grade = ProcessQualityBenchmark.grade(
        task,
        result,
        {"process": {"reuse_attempted": 1, "reuse_found": 1}},
    )
    assert grade.passed is True
    assert grade.score == 1.0


@pytest.mark.asyncio
async def test_repo_pytest_benchmark_is_reproducible_and_filters_cache_files() -> None:
    async def fix_add(task, root):
        (root / "calculator.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a + b\n",
            encoding="utf-8",
        )
        return AgentResult(status=ResultStatus.SUCCESS, output="fixed"), {"efficiency": {"tool_call_count": 1}}

    result = await RepoPytestBenchmark().run(fix_add)
    assert result.baseline_failed is True
    assert result.tests_passed is True
    assert all("__pycache__" not in path for path in result.changed_files)
    assert result.run_score == {"efficiency": {"tool_call_count": 1}}


def test_swebench_private_test_patch_is_applied(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "calculator.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@synapse.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Synapse Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    agent_patch = (
        "diff --git a/calculator.py b/calculator.py\n"
        "--- a/calculator.py\n+++ b/calculator.py\n"
        "@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a + b\n+    return a + b\n"
    )
    private_patch = (
        "diff --git a/test_private.py b/test_private.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n+++ b/test_private.py\n"
        "@@ -0,0 +1,2 @@\n+from calculator import add\n+def test_add(): assert add(2, 3) == 5\n"
    )
    result = SWEBenchAdapter.execute(
        str(repo), commit, agent_patch, {}, private_test_patch=private_patch, timeout=60,
    )
    assert result.private_tests_applied is True
    assert result.passed is True, result.output
