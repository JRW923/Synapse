"""Terminal-Bench style adapters and a deterministic local smoke benchmark.

The official Terminal-Bench runner owns container images and dataset graders;
Synapse intentionally provides only a thin task/trajectory/grader adapter so
the project stays installable offline and does not vendor external data.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from synapse.eval.runner import Benchmark, BenchmarkTask, TaskGrade
from synapse.protocols.planner import AgentResult


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
                "dataset_manifest": {
                    "source": "user-provided Terminal-Bench-compatible export",
                    "license": "unknown",
                    "grader_timeouts_seconds": [120],
                },
            },
        )

    @staticmethod
    def grade(task, agent_result: AgentResult, run_score: dict | None) -> TaskGrade:
        facts = (run_score or {}).get("terminal", {})
        grader_error = facts.get("grader_error")
        if grader_error:
            raise RuntimeError(str(grader_error))
        passed = bool(facts.get("passed"))
        score = 1.0 if passed else 0.0
        return TaskGrade(
            passed=passed,
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
    def require_trusted_host_execution(
        tasks: list[BenchmarkTask], trusted_host_execution: bool = False,
    ) -> None:
        """Reject task-declared host commands before an Agent run starts."""
        requires_host = any(
            task.metadata.get("grader_command") not in (None, "", [])
            for task in tasks
        )
        if requires_host and trusted_host_execution is not True:
            raise RuntimeError(
                "Terminal grader host execution is disabled; pass "
                "trusted_host_execution=True only for trusted datasets"
            )

    @staticmethod
    def preflight(
        task: BenchmarkTask,
        workspace: str | Path,
        *,
        trusted_host_execution: bool = False,
    ) -> dict:
        """Validate the task grader against a clean copy before Agent work.

        The copy prevents a command grader from leaving caches or generated
        files in the measured workspace.  A task that already passes is
        rejected instead of being reported as an Agent success; a missing or
        broken grader is an infrastructure/configuration error, not a model
        failure.
        """
        metadata = task.metadata
        expected_files = metadata.get("expected_files")
        raw_command = metadata.get("grader_command")
        has_expected = isinstance(expected_files, dict) and bool(expected_files)
        has_command = raw_command not in (None, "", [])
        if not has_expected and not has_command:
            raise RuntimeError(
                f"task {task.id} has no external grader; declare expected_files "
                "or grader_command"
            )

        root = Path(workspace).expanduser().resolve()
        if not root.is_dir():
            raise RuntimeError(f"workspace does not exist: {root}")
        with tempfile.TemporaryDirectory(prefix="synapse-terminal-preflight-") as tmp:
            copy_root = Path(tmp) / "workspace"
            shutil.copytree(root, copy_root)
            facts = TerminalBenchAdapter.grade_workspace(
                task,
                copy_root,
                trusted_host_execution=trusted_host_execution,
            )
        if facts.get("grader_error"):
            raise RuntimeError(
                f"task {task.id} grader preflight failed: {facts['grader_error']}"
            )
        if facts.get("passed"):
            raise RuntimeError(
                f"task {task.id} baseline already passes; refusing to score it"
            )
        return {
            "passed": False,
            "grader": facts.get("grader", "unknown"),
            "missing": facts.get("missing", []),
            "mismatched": facts.get("mismatched", []),
        }

    @staticmethod
    def grade_workspace(
        task: BenchmarkTask,
        workspace: str | Path,
        *,
        trusted_host_execution: bool = False,
    ) -> dict:
        """Run a task's declared command or deterministic file assertions."""
        TerminalBenchAdapter.require_trusted_host_execution(
            [task], trusted_host_execution,
        )
        root = Path(workspace).expanduser().resolve()
        expected_files = task.metadata.get("expected_files", {})
        missing = []
        mismatched = []
        for relative, expected in expected_files.items():
            target = (root / str(relative)).resolve()
            if not target.is_relative_to(root):
                return {
                    "passed": False,
                    "grader": "filesystem",
                    "grader_error": "expected file path escapes workspace",
                    "output": "path escapes workspace",
                }
            if not target.is_file():
                missing.append(str(relative))
                continue
            if expected is not None and target.read_text(encoding="utf-8") != str(expected):
                mismatched.append(str(relative))
        raw_command = task.metadata.get("grader_command")
        command: list[str] = []
        if isinstance(raw_command, str) and raw_command.strip():
            try:
                command = shlex.split(raw_command, posix=os.name != "nt")
            except ValueError:
                return {
                    "passed": False, "grader": "command", "output": "invalid grader command",
                    "grader_error": "invalid grader command",
                    "missing": missing, "mismatched": mismatched,
                }
        elif isinstance(raw_command, (list, tuple)):
            if any(not isinstance(item, str) or not item for item in raw_command):
                return {
                    "passed": False, "grader": "command", "output": "invalid grader argv",
                    "grader_error": "invalid grader argv",
                    "missing": missing, "mismatched": mismatched,
                }
            command = list(raw_command)
        elif raw_command not in (None, ""):
            return {
                "passed": False, "grader": "command", "output": "invalid grader command",
                "grader_error": "invalid grader command",
                "missing": missing, "mismatched": mismatched,
            }
        if command:
            safe_env_keys = {
                "HOME", "LANG", "LC_ALL", "PATH", "PATHEXT", "PYTHONIOENCODING",
                "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "USERPROFILE", "WINDIR",
            }
            completed = subprocess.run(
                command,
                cwd=root,
                shell=False,
                capture_output=True,
                text=True,
                timeout=int(task.metadata.get("timeout", 120)),
                env={
                    key: value for key, value in os.environ.items()
                    if key.upper() in safe_env_keys
                },
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
                "dataset_manifest": {
                    "version": "1",
                    "source": "bundled deterministic fixture",
                    "license": "not_declared",
                },
            },
        )
