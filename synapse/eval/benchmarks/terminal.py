"""Terminal-Bench style adapters and a deterministic local smoke benchmark.

The official Terminal-Bench runner owns container images and dataset graders;
Synapse intentionally provides only a thin task/trajectory/grader adapter so
the project stays installable offline and does not vendor external data.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from synapse.eval.runner import Benchmark, BenchmarkTask, TaskGrade
from synapse.protocols.planner import AgentResult, ResultStatus


class TerminalBenchAdapter:
    """Load Terminal-Bench-like tasks and grade isolated workspaces."""

    NAME = "terminal_bench"

    @classmethod
    def tasks(
        cls,
        dataset_path: str | Path | None = None,
        limit: int | None = None,
    ) -> list[BenchmarkTask]:
        """Load JSON/JSONL tasks without requiring the official dataset package."""
        if dataset_path is None:
            return []
        path = Path(dataset_path).expanduser()
        raw = json.loads(path.read_text(encoding="utf-8")) if path.suffix == ".json" else [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        if isinstance(raw, dict):
            raw = raw.get("tasks", raw.get("data", []))
        tasks: list[BenchmarkTask] = []
        for item in raw:
            task_id = str(item.get("task_id") or item.get("id") or len(tasks))
            description = str(
                item.get("instruction") or item.get("task") or item.get("description") or ""
            ).strip()
            if not description:
                continue
            metadata = dict(item)
            metadata.setdefault("category", "terminal")
            metadata.setdefault("harness", "terminal-bench")
            tasks.append(BenchmarkTask(task_id, description, metadata=metadata))
            if limit is not None and len(tasks) >= max(0, limit):
                break
        return tasks

    @classmethod
    def benchmark(cls, tasks: list[BenchmarkTask]) -> Benchmark:
        return Benchmark(
            name=cls.NAME,
            tasks=tasks,
            grader=cls.grade,
            metadata={
                "harness": "terminal-bench-compatible",
                "official_runner": "external",
                "trajectory": "event_bus_and_run_score",
            },
        )

    @staticmethod
    def grade(task, agent_result: AgentResult, run_score: dict | None) -> TaskGrade:
        facts = (run_score or {}).get("terminal", {})
        passed = bool(facts.get("passed"))
        status_ok = agent_result.status == ResultStatus.SUCCESS
        score = 1.0 if passed and status_ok else (0.5 if passed else 0.0)
        return TaskGrade(
            passed=passed and status_ok,
            score=score,
            reason="terminal grader passed" if passed else "terminal grader failed",
            details={
                "agent_status": agent_result.status.value,
                "grader": facts.get("grader", "unknown"),
                "command": facts.get("command", ""),
                "grader_output": facts.get("output", "")[-2000:],
            },
        )

    @staticmethod
    def grade_workspace(task: BenchmarkTask, workspace: str | Path) -> dict:
        """Run a task's declared command or deterministic file assertions."""
        root = Path(workspace).expanduser().resolve()
        expected_files = task.metadata.get("expected_files", {})
        missing = []
        mismatched = []
        for relative, expected in expected_files.items():
            target = (root / str(relative)).resolve()
            if not target.is_relative_to(root):
                return {"passed": False, "grader": "filesystem", "output": "path escapes workspace"}
            if not target.is_file():
                missing.append(str(relative))
                continue
            if expected is not None and target.read_text(encoding="utf-8") != str(expected):
                mismatched.append(str(relative))
        command = str(task.metadata.get("grader_command", "")).strip()
        if command:
            completed = subprocess.run(
                command,
                cwd=root,
                shell=True,
                capture_output=True,
                text=True,
                timeout=int(task.metadata.get("timeout", 120)),
            )
            output = (completed.stdout + completed.stderr)[-4000:]
            return {
                "passed": completed.returncode == 0 and not missing and not mismatched,
                "grader": "command",
                "command": command,
                "returncode": completed.returncode,
                "output": output,
                "missing": missing,
                "mismatched": mismatched,
            }
        return {
            "passed": not missing and not mismatched,
            "grader": "filesystem",
            "output": f"missing={missing}; mismatched={mismatched}",
            "missing": missing,
            "mismatched": mismatched,
        }


class TerminalSmokeBenchmark:
    """Offline fixture exercising terminal action plus an isolated grader."""

    @classmethod
    def tasks(cls) -> list[BenchmarkTask]:
        return [
            BenchmarkTask(
                id="terminal-create-marker",
                description=(
                    "Use the terminal shell to create marker.txt with exactly "
                    "the text SYNAPSE_TERMINAL_OK followed by a newline. "
                    "Verify it from the terminal before finishing."
                ),
                metadata={
                    "category": "terminal",
                    "harness": "terminal-bench-smoke",
                    "expected_files": {"marker.txt": "SYNAPSE_TERMINAL_OK\n"},
                },
            ),
        ]

    @classmethod
    def benchmark(cls) -> Benchmark:
        return Benchmark(
            name="terminal_smoke",
            tasks=cls.tasks(),
            grader=TerminalBenchAdapter.grade,
            metadata={
                "harness": "terminal-bench-compatible",
                "isolation": "temporary_workspace",
                "official_runner": "not_required",
            },
        )
