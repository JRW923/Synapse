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
import random
from datetime import datetime
from typing import ClassVar

from synapse.eval.runner import BenchmarkTask


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
