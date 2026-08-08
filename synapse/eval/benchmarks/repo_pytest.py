"""Small reproducible benchmark backed by a temporary Git repo and pytest."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Awaitable, Callable

from synapse.protocols.planner import AgentResult


@dataclass
class RepoPytestResult:
    baseline_failed: bool
    tests_passed: bool
    changed_files: list[str] = field(default_factory=list)
    pytest_output: str = ""
    agent_result: AgentResult | None = None


class RepoPytestBenchmark:
    """Run one fix task in an isolated repo, then grade the Git diff with pytest."""

    task = "Fix add() so the existing pytest suite passes. Do not change the tests."

    async def run(
        self,
        run_agent: Callable[[str, Path], Awaitable[AgentResult]],
    ) -> RepoPytestResult:
        with tempfile.TemporaryDirectory(prefix="synapse-bench-") as tmp:
            root = Path(tmp)
            self._create_repo(root)
            baseline = await asyncio.to_thread(self._pytest, root)
            agent_result = await run_agent(self.task, root)
            final = await asyncio.to_thread(self._pytest, root)
            changed = subprocess.run(
                ["git", "status", "--short"], cwd=root,
                capture_output=True, text=True, check=True,
            ).stdout.splitlines()
            return RepoPytestResult(
                baseline_failed=baseline.returncode != 0,
                tests_passed=final.returncode == 0,
                changed_files=changed,
                pytest_output=(final.stdout + final.stderr)[-4000:],
                agent_result=agent_result,
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
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q"], cwd=root,
            capture_output=True, text=True, timeout=60,
        )
