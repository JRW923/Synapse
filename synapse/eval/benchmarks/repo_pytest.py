"""Small reproducible benchmark backed by a temporary Git repo and pytest."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Awaitable, Callable

from synapse.protocols.planner import AgentResult, ResultStatus
from synapse.eval.runner import Benchmark, BenchmarkTask, TaskGrade


@dataclass
class RepoPytestResult:
    baseline_failed: bool
    tests_passed: bool
    changed_files: list[str] = field(default_factory=list)
    pytest_output: str = ""
    agent_result: AgentResult | None = None
    run_score: dict | None = None

    def to_dict(self) -> dict:
        """Return the functional grading facts used by the eval report."""
        return {
            "baseline_failed": self.baseline_failed,
            "tests_passed": self.tests_passed,
            "changed_files": self.changed_files,
            "pytest_output": self.pytest_output[-4000:],
            "run_score": self.run_score,
        }


class RepoPytestBenchmark:
    """Run one fix task in an isolated repo, then grade the Git diff with pytest."""

    task = "Fix add() so the existing pytest suite passes. Do not change the tests."

    @classmethod
    def benchmark(cls) -> Benchmark:
        """Return the task and grader metadata for the generic runner."""
        return Benchmark(
            name="repo_pytest",
            tasks=[BenchmarkTask(
                id="calculator-add",
                description=cls.task,
                metadata={"category": "functional", "difficulty": "easy"},
            )],
            grader=cls.grade,
            metadata={"isolation": "temporary_git_repo", "grader": "pytest"},
        )

    @staticmethod
    def grade(task, agent_result: AgentResult, run_score: dict | None) -> TaskGrade:
        """Grade a callback result produced by :meth:`run`.

        The callback places ``RepoPytestResult.to_dict()`` under the
        ``repo_pytest`` key in ``run_score``. This keeps the generic runner
        independent of temporary-repository internals.
        """
        facts = (run_score or {}).get("repo_pytest", {})
        passed = bool(facts.get("tests_passed"))
        agent_ok = agent_result.status == ResultStatus.SUCCESS
        score = 1.0 if passed and agent_ok else 0.0
        reason = "pytest suite passed" if passed else "pytest suite still failing"
        return TaskGrade(
            passed=passed and agent_ok,
            score=score,
            reason=reason,
            details={
                "agent_status": agent_result.status.value,
                "tests_passed": passed,
                "changed_files": facts.get("changed_files", []),
            },
        )

    async def run(
        self,
        run_agent: Callable[[str, Path], Awaitable[AgentResult | tuple[AgentResult, dict]]],
    ) -> RepoPytestResult:
        with tempfile.TemporaryDirectory(prefix="synapse-bench-") as tmp:
            root = Path(tmp)
            self._create_repo(root)
            baseline = await asyncio.to_thread(self._pytest, root)
            execution = await run_agent(self.task, root)
            run_score = None
            if isinstance(execution, tuple) and len(execution) == 2:
                agent_result, run_score = execution
            else:
                agent_result = execution
            final = await asyncio.to_thread(self._pytest, root)
            changed = subprocess.run(
                ["git", "status", "--short"], cwd=root,
                capture_output=True, text=True, check=True,
            ).stdout.splitlines()
            return RepoPytestResult(
                baseline_failed=baseline.returncode != 0,
                tests_passed=final.returncode == 0,
                changed_files=[line for line in changed if "__pycache__" not in line and not line.endswith(".pyc")],
                pytest_output=(final.stdout + final.stderr)[-4000:],
                agent_result=agent_result,
                run_score=run_score,
            )

    @staticmethod
    def _create_repo(root: Path) -> None:
        (root / "calculator.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a - b\n",
            encoding="utf-8",
        )
        (root / "test_calculator.py").write_text(
            "from calculator import add\n\n"
            "def test_add():\n    assert add(2, 3) == 5\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "bench@synapse.local"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Synapse Benchmark"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "benchmark fixture"], cwd=root, check=True)

    @staticmethod
    def _pytest(root: Path) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q"], cwd=root,
            capture_output=True, text=True, timeout=60, env=env,
        )
