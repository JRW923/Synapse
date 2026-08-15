"""Tests for SWEBenchAdapter and ProcessQualityBenchmark."""

import json
import pytest
import subprocess
from pathlib import Path

from synapse.eval.runner import Benchmark, BenchmarkRunner, BenchmarkTask
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


def test_functional_graders_ignore_agent_reported_status() -> None:
    partial = AgentResult(status=ResultStatus.PARTIAL, output="not sure")
    repo_grade = RepoPytestBenchmark.grade(
        BenchmarkTask(id="repo", description="fix"),
        partial,
        {"repo_pytest": {"tests_passed": True}},
    )
    swe_grade = SWEBenchAdapter.grade(
        BenchmarkTask(id="swe", description="fix"),
        partial,
        {"swebench": {"applied": True, "tests_passed": True}},
    )

    assert repo_grade.passed is True
    assert swe_grade.passed is True
    assert repo_grade.details["agent_status"] == "partial"
    assert swe_grade.details["agent_status"] == "partial"


def test_swebench_manifest_does_not_persist_local_dataset_path(tmp_path) -> None:
    dataset = tmp_path / "private" / "tasks.jsonl"
    dataset.parent.mkdir()
    dataset.write_text(
        json.dumps({"instance_id": "one", "problem_statement": "Fix it"}) + "\n",
        encoding="utf-8",
    )

    benchmark = SWEBenchAdapter.benchmark(dataset)
    assert benchmark.metadata["dataset"]["name"] == "tasks.jsonl"
    assert len(benchmark.metadata["dataset"]["sha256"]) == 64
    assert str(tmp_path) not in json.dumps(benchmark.metadata)


@pytest.mark.asyncio
async def test_repo_pytest_benchmark_is_reproducible_and_filters_cache_files() -> None:
    async def fix_add(task, root):
        (root / "calculator.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a + b\n",
            encoding="utf-8",
        )
        return AgentResult(status=ResultStatus.SUCCESS, output="fixed"), {"efficiency": {"tool_call_count": 1}}

    result = await RepoPytestBenchmark().run(
        fix_add, trusted_host_execution=True,
    )
    assert result.baseline_failed is True
    assert result.tests_passed is True
    assert all("__pycache__" not in path for path in result.changed_files)
    assert result.run_score == {"efficiency": {"tool_call_count": 1}}


@pytest.mark.asyncio
async def test_repo_pytest_refuses_untrusted_host_execution():
    called = False

    async def run_agent(_task, _root):
        nonlocal called
        called = True
        raise AssertionError("agent must not run before host trust")

    with pytest.raises(RuntimeError, match="trusted_host_execution=True"):
        await RepoPytestBenchmark().run(run_agent)
    assert called is False


def test_repo_pytest_grader_does_not_inherit_host_secrets(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("SYNAPSE_PRIVATE_API_TOKEN", "must-not-leak")
    (tmp_path / "test_env.py").write_text(
        "import os\n\n"
        "def test_no_secret():\n"
        "    assert os.getenv('SYNAPSE_PRIVATE_API_TOKEN') is None\n",
        encoding="utf-8",
    )

    completed = RepoPytestBenchmark._pytest(tmp_path)

    assert completed.returncode == 0, completed.stdout + completed.stderr


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
        trusted_host_execution=True,
    )
    assert result.private_tests_applied is True
    assert result.passed is True, result.output


def test_swebench_execute_requires_explicit_host_trust(monkeypatch):
    def must_not_run(*_args, **_kwargs):
        raise AssertionError("host subprocess must not start")

    monkeypatch.setattr(subprocess, "run", must_not_run)

    with pytest.raises(RuntimeError, match="trusted_host_execution=True"):
        SWEBenchAdapter.execute(
            "https://example.invalid/repo.git",
            "deadbeef",
            "non-empty patch",
            {"test_private.py": "def test_private(): pass\n"},
        )


@pytest.mark.asyncio
async def test_swebench_host_refusal_is_not_a_verified_agent_failure():
    task = BenchmarkTask(id="one", description="Fix it", metadata={"category": "swebench"})
    benchmark = Benchmark(name="swebench", tasks=[task], grader=SWEBenchAdapter.grade)

    async def reject_before_agent(_task):
        SWEBenchAdapter.execute(
            "https://example.invalid/repo.git",
            "deadbeef",
            "non-empty patch",
            {"test_private.py": "def test_private(): pass\n"},
        )

    async def unused(_description):
        raise AssertionError("task-aware preflight should be used")

    result = await BenchmarkRunner().run(benchmark, unused, task_runner=reject_before_agent)

    assert result.scored_attempt_total == 0
    assert result.results[0].verification_status == "not_graded"
    assert "trusted_host_execution=True" in (result.results[0].error or "")
