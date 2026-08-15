"""SWE-bench Adapter with anti-contamination measures.

Provides ``SWEBenchAdapter`` that wraps SWE-bench tasks with three
anti-contamination techniques:

1. **Template variation** (:meth:`mutate_task`):
   Applies synonym replacement and instruction reordering based on a
   deterministic seed to change surface form without altering semantics.

2. **Time slicing** (:meth:`filter_by_date`):
   Filters tasks to a cutoff date so that models cannot have memorized
   solutions that were published after their training cut-off.

3. **Private test validation** (:meth:`validate_with_private_tests`):
   Validates patches against a private test suite to ensure the benchmark
   measures actual problem-solving rather than pattern matching against
   public test cases.
"""

from __future__ import annotations

import hashlib
import json
import random
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from synapse.eval.runner import Benchmark, BenchmarkTask, TaskGrade
from synapse.protocols.planner import AgentResult


@dataclass
class SWEBenchExecution:
    applied: bool
    passed: bool
    returncode: int = -1
    output: str = ""
    changed_files: list[str] = field(default_factory=list)
    private_tests_applied: bool = False


# ---------------------------------------------------------------------------
# Synonym / paraphrase tables
# ---------------------------------------------------------------------------

_SYNONYMS: dict[str, list[str]] = {
    "fix": ["resolve", "repair", "correct", "patch"],
    "bug": ["defect", "issue", "error", "fault"],
    "implement": ["build", "create", "develop", "add"],
    "add": ["introduce", "include", "attach", "insert"],
    "change": ["modify", "alter", "adjust", "revise"],
    "remove": ["delete", "drop", "strip", "eliminate"],
    "update": ["upgrade", "refresh", "revise", "modernize"],
    "check": ["verify", "validate", "inspect", "confirm"],
    "find": ["locate", "discover", "identify", "detect"],
    "improve": ["enhance", "optimize", "refine", "upgrade"],
    "function": ["method", "routine", "procedure", "operation"],
    "module": ["component", "package", "unit", "library"],
    "error": ["failure", "exception", "fault", "problem"],
    "test": ["spec", "verify", "validate", "check"],
    "performance": ["speed", "efficiency", "throughput", "responsiveness"],
}

# Instruction prefixes that can be reordered (by sentence boundary)
_REORDERABLE_PREFIXES: list[str] = [
    "Please ",
    "Make sure to ",
    "Ensure that ",
    "Remember to ",
    "Do not forget to ",
    "You should ",
    "You must ",
    "It is important to ",
]


# ---------------------------------------------------------------------------
# SWEBenchAdapter
# ---------------------------------------------------------------------------


class SWEBenchAdapter:
    """Adapts SWE-bench tasks with anti-contamination measures.

    All methods are static so callers can compose them as needed::

        >>> adapter = SWEBenchAdapter
        >>> clean_task = adapter.mutate_task(task, seed=42)
        >>> recent = adapter.filter_by_date(all_tasks, cutoff=datetime(2024, 1, 1))
        >>> is_valid = adapter.validate_with_private_tests(patch, private_test_suite)
    """

    # ------------------------------------------------------------------
    # dataset loading
    # ------------------------------------------------------------------

    @classmethod
    def tasks(
        cls,
        dataset_path: str | Path | None = None,
        limit: int | None = None,
    ) -> list[BenchmarkTask]:
        """Load SWE-bench-style JSONL tasks without pretending to ship data.

        The repository deliberately does not vendor the benchmark dataset.
        Callers must pass a local JSONL export; an omitted path returns an
        empty list so the CLI can explain the missing external fixture.
        """
        if dataset_path is None:
            return []
        path = Path(dataset_path).expanduser()
        tasks: list[BenchmarkTask] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                item = json.loads(line)
                task_id = str(item.get("instance_id") or item.get("id") or len(tasks))
                description = str(item.get("problem_statement") or item.get("description") or "")
                if not description:
                    continue
                metadata = dict(item)
                metadata.setdefault("category", "swebench")
                tasks.append(BenchmarkTask(task_id, description, metadata=metadata))
                if limit is not None and len(tasks) >= max(0, limit):
                    break
        return tasks

    @classmethod
    def benchmark(
        cls,
        dataset_path: str | Path,
        limit: int | None = None,
    ) -> Benchmark:
        """Build a scored benchmark from a local SWE-bench JSONL export."""
        dataset = Path(dataset_path).expanduser().resolve()
        tasks = cls.tasks(dataset, limit)
        return Benchmark(
            name="swebench",
            tasks=tasks,
            grader=cls.grade,
            metadata={
                "dataset": {
                    "name": dataset.name,
                    "sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
                },
                "functional_grader": "isolated_patch_private_tests",
                "dataset_manifest": {
                    "source": "user-provided SWE-bench-compatible export",
                    "license": "unknown",
                    "grader_timeouts_seconds": [900],
                },
            },
        )

    @staticmethod
    def grade(task, agent_result: AgentResult, run_score: dict | None) -> TaskGrade:
        """Grade an isolated patch using apply/test evidence, not status alone."""
        facts = (run_score or {}).get("swebench", {})
        applied = bool(facts.get("applied"))
        tested = bool(facts.get("tests_passed"))
        score = (0.4 if applied else 0.0) + (0.6 if tested else 0.0)
        passed = applied and tested
        reason = "private tests passed" if passed else (
            "patch applied but private tests failed" if applied else "patch could not be applied"
        )
        return TaskGrade(
            passed=passed,
            score=score,
            reason=reason,
            details={
                "agent_status": agent_result.status.value,
                "patch_applied": applied,
                "tests_passed": tested,
                "changed_files": facts.get("changed_files", []),
            },
        )

    @staticmethod
    def extract_patch(repo_root: str | Path) -> str:
        """Return a binary-safe Git patch, including newly-created files."""
        root = Path(repo_root).expanduser().resolve()
        subprocess.run(
            ["git", "add", "--intent-to-add", "--all"],
            cwd=root, capture_output=True, text=True, check=False,
        )
        result = subprocess.run(
            ["git", "diff", "--binary", "HEAD", "--"],
            cwd=root, capture_output=True, text=True, check=False,
        )
        return result.stdout

    # ------------------------------------------------------------------
    # mutate_task
    # ------------------------------------------------------------------

    @staticmethod
    def mutate_task(task: BenchmarkTask, seed: int = 0) -> BenchmarkTask:
        """Apply template variation to *task* using *seed* for reproducibility.

        Two mutations are applied:

        1. **Synonym replacement**: words in the description that match
           entries in the built-in synonym table are probabilistically
           replaced (one in two words, guided by the seed).

        2. **Sentence reordering**: instruction-prefixed sentences are
           shuffled while keeping the core task sentence first.

        The result is a new ``BenchmarkTask`` with a modified ``description``
        but the same ``id`` and ``metadata``.

        Parameters
        ----------
        task:
            The original benchmark task.
        seed:
            Deterministic seed for reproducible variation.

        Returns
        -------
        BenchmarkTask
            A new task whose description has been mutated.
        """
        rng = random.Random(seed)
        text = task.description

        # --- 1. Synonym replacement ---
        words = text.split()
        mutated_words: list[str] = []
        for word in words:
            lower = word.lower().rstrip(".,;:!?()[]{}\"'")
            if lower in _SYNONYMS and rng.random() < 0.5:
                replacement = rng.choice(_SYNONYMS[lower])
                # Preserve capitalization and trailing punctuation
                if word[0].isupper():
                    replacement = replacement[0].upper() + replacement[1:]
                # Preserve trailing punctuation
                suffix = word[len(lower):]
                mutated_words.append(replacement + suffix)
            else:
                mutated_words.append(word)

        new_text = " ".join(mutated_words)

        # --- 2. Sentence reordering (instruction prefixes) ---
        sentences = _split_sentences(new_text)
        if len(sentences) >= 2:
            # Keep first sentence (core task) fixed
            first = sentences[0]
            rest = sentences[1:]
            rng.shuffle(rest)
            new_text = " ".join([first] + rest)

        return BenchmarkTask(
            id=task.id,
            description=new_text,
            metadata=dict(task.metadata),
            expected_process_scores=dict(task.expected_process_scores),
        )

    # ------------------------------------------------------------------
    # filter_by_date
    # ------------------------------------------------------------------

    @staticmethod
    def filter_by_date(
        tasks: list[BenchmarkTask],
        cutoff: datetime,
    ) -> list[BenchmarkTask]:
        """Return tasks whose ``metadata["date"]`` is before *cutoff*.

        Tasks without a ``"date"`` key in metadata are included
        (conservative: assume they might be before the cutoff).

        Parameters
        ----------
        tasks:
            Full list of benchmark tasks.
        cutoff:
            Only tasks created before this date are retained.

        Returns
        -------
        list[BenchmarkTask]
            Filtered task list.
        """
        filtered: list[BenchmarkTask] = []
        for task in tasks:
            date_str = task.metadata.get("date")
            if date_str is None:
                filtered.append(task)  # no date → include
                continue
            try:
                task_date = datetime.fromisoformat(date_str)
            except (ValueError, TypeError):
                filtered.append(task)  # unparseable → include
                continue
            if task_date < cutoff:
                filtered.append(task)
        return filtered

    # ------------------------------------------------------------------
    # validate_with_private_tests
    # ------------------------------------------------------------------

    @staticmethod
    def validate_with_private_tests(
        patch: str,
        private_tests: str,
    ) -> bool:
        """Validate *patch* against a private test suite.

        This is a **placeholder** that reports whether the private test
        content is non-empty and the patch is non-trivial.  In a production
        deployment this would execute the private test suite inside a
        sandboxed environment.

        Currently this method is a structural guard: it returns ``True``
        only when both *patch* and *private_tests* contain real content.
        This prevents accidentally passing empty test suites.

        Parameters
        ----------
        patch:
            The generated diff / patch content.
        private_tests:
            The private test suite content (e.g. pytest file contents).

        Returns
        -------
        bool
            ``True`` if both *patch* and *private_tests* are non-empty.
        """
        return bool(patch.strip()) and bool(private_tests.strip())

    @staticmethod
    def require_trusted_host_execution(trusted_host_execution: bool = False) -> None:
        """Reject host-side checkout and grader commands unless explicitly trusted."""
        if trusted_host_execution is not True:
            raise RuntimeError(
                "SWE-bench host execution is disabled; pass "
                "trusted_host_execution=True only for trusted datasets"
            )

    @staticmethod
    def execute(
        repo_url: str,
        base_commit: str,
        patch: str,
        private_tests: dict[str, str],
        test_command: list[str] | None = None,
        timeout: int = 900,
        private_test_patch: str = "",
        *,
        trusted_host_execution: bool = False,
    ) -> SWEBenchExecution:
        """Clone, checkout, apply a patch, inject private tests, and execute them."""
        SWEBenchAdapter.require_trusted_host_execution(trusted_host_execution)
        if not patch.strip() or (not private_tests and not private_test_patch.strip()):
            return SWEBenchExecution(applied=False, passed=False, output="empty patch or tests")
        with tempfile.TemporaryDirectory(prefix="synapse-swebench-") as tmp:
            root = Path(tmp) / "repo"
            try:
                subprocess.run(
                    ["git", "clone", "--quiet", repo_url, str(root)],
                    capture_output=True, text=True, check=True, timeout=timeout,
                )
                subprocess.run(
                    ["git", "checkout", "--quiet", base_commit], cwd=root,
                    capture_output=True, text=True, check=True, timeout=60,
                )
                applied = subprocess.run(
                    ["git", "apply", "--whitespace=nowarn", "-"], cwd=root,
                    input=patch, capture_output=True, text=True, timeout=60,
                )
                if applied.returncode != 0:
                    return SWEBenchExecution(
                        applied=False, passed=False,
                        returncode=applied.returncode, output=applied.stderr,
                    )
                private_tests_applied = False
                if private_test_patch.strip():
                    test_patch = subprocess.run(
                        ["git", "apply", "--whitespace=nowarn", "-"], cwd=root,
                        input=private_test_patch, capture_output=True, text=True, timeout=60,
                    )
                    if test_patch.returncode != 0:
                        return SWEBenchExecution(
                            applied=True, passed=False,
                            output=f"private test patch failed: {test_patch.stderr}",
                        )
                    private_tests_applied = True
                for relative, content in private_tests.items():
                    target = (root / relative).resolve()
                    if not target.is_relative_to(root.resolve()):
                        raise ValueError(f"Private test path escapes repo: {relative}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                    private_tests_applied = True
                command = test_command or [sys.executable, "-m", "pytest", "-q"]
                tested = subprocess.run(
                    command, cwd=root, capture_output=True, text=True, timeout=timeout,
                )
                changed = subprocess.run(
                    ["git", "status", "--short"], cwd=root,
                    capture_output=True, text=True, check=True,
                ).stdout.splitlines()
                return SWEBenchExecution(
                    applied=True,
                    passed=tested.returncode == 0,
                    returncode=tested.returncode,
                    output=(tested.stdout + tested.stderr)[-8000:],
                    changed_files=changed,
                    private_tests_applied=private_tests_applied,
                )
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                return SWEBenchExecution(applied=False, passed=False, output=str(exc))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _split_sentences(text: str) -> list[str]:
    """Rudimentary sentence splitter — splits on ``. `` and ``! `` boundaries."""
    # Replace period-space and exclamation-space with a marker, then split
    result: list[str] = []
    current: list[str] = []
    for ch in text:
        current.append(ch)
        if ch in (".", "!") and len(current) >= 2:
            result.append("".join(current).strip())
            current = []
    if current:
        remaining = "".join(current).strip()
        if remaining:
            result.append(remaining)
    # Merge back fragments that don't end with punctuation
    merged: list[str] = []
    for part in result:
        if merged and not merged[-1].endswith((".", "!")):
            merged[-1] = merged[-1] + " " + part
        else:
            merged.append(part)
    return merged if merged else [text]
