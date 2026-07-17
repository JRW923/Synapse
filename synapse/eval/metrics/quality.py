"""QualityMetrics — code complexity, duplication, function-length violations,
test coverage, and lint errors.

Subscribes to EventBus events and aggregates code-quality metrics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from synapse.core.events import EventBus
from synapse.protocols.events import BaseEvent


# ---- Snapshot ---------------------------------------------------------------


@dataclass
class QualitySnapshot:
    """Point-in-time snapshot of code-quality metrics."""

    complexity_delta: float = 0.0
    duplication_rate: float = 0.0
    function_length_violations: int = 0
    test_coverage_delta: float = 0.0
    lint_errors_introduced: int = 0


# ---- Collector --------------------------------------------------------------


class QualityMetrics:
    """Collects code-quality metrics from EventBus events.

    Subscribes to:
    - ``file_written``        — complexity delta, duplication rate,
                                function-length violations, test coverage delta
    - ``tool_call_completed`` — lint errors introduced

    Parameters
    ----------
    bus:
        The EventBus to subscribe to. If ``None``, no subscription is
        performed (useful for testing / standalone usage).
    """

    _WATCHED_EVENTS = frozenset({
        "file_written",
        "tool_call_completed",
    })

    # Tools that indicate lint/format operations
    _LINT_TOOLS = frozenset({"lint", "flake8", "pylint", "eslint", "ruff", "check"})

    # Files considered test files
    _TEST_FILE_PATTERN = re.compile(
        r"(^|[\\/])(tests?[\\/]|test_|spec_|_test\.|_spec\.)",
    )

    # Threshold: files larger than this (bytes) suggest possible function-length
    # violations (heuristic — real systems would parse ASTs).
    _FUNCTION_LENGTH_THRESHOLD_BYTES = 2048

    # Threshold: files written with > this ratio of total bytes may indicate
    # code duplication.
    _DUPLICATION_SIZE_RATIO_THRESHOLD = 0.7

    def __init__(self, bus: EventBus | None) -> None:
        self._bus = bus
        self.reset()

        if bus is not None:
            for event_type in self._WATCHED_EVENTS:
                bus.subscribe(event_type, self._on_event)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Reset all accumulated metrics to zero/empty."""
        self._source_files_written = 0
        self._test_files_written = 0
        self._total_bytes_written = 0
        self._largest_file_bytes = 0
        self._function_length_violations = 0
        self._lint_errors = 0

    def snapshot(self) -> QualitySnapshot:
        """Return a point-in-time snapshot of all collected quality metrics."""
        total_files = self._source_files_written + self._test_files_written

        # Complexity delta: heuristic based on files touched and bytes written.
        # More files + larger files = higher complexity increase.
        complexity_delta = 0.0
        if total_files > 0:
            avg_bytes = self._total_bytes_written / max(total_files, 1)
            complexity_delta = round(
                (total_files * 0.3) + (min(avg_bytes / 1024.0, 10.0) * 0.7),
                4,
            )

        # Duplication rate: high ratio of largest file to total suggests
        # potential duplication (heuristic).
        duplication_rate = 0.0
        if self._total_bytes_written > 0:
            raw_ratio = self._largest_file_bytes / self._total_bytes_written
            duplication_rate = round(min(raw_ratio / self._DUPLICATION_SIZE_RATIO_THRESHOLD, 1.0), 4)

        # Test coverage delta: ratio of test files to source files (crude proxy).
        test_coverage_delta = 0.0
        if self._source_files_written > 0:
            test_coverage_delta = round(
                self._test_files_written / max(self._source_files_written, 1),
                4,
            )

        return QualitySnapshot(
            complexity_delta=complexity_delta,
            duplication_rate=duplication_rate,
            function_length_violations=self._function_length_violations,
            test_coverage_delta=test_coverage_delta,
            lint_errors_introduced=self._lint_errors,
        )

    # ------------------------------------------------------------------
    # Event handler
    # ------------------------------------------------------------------

    async def _on_event(self, event: BaseEvent) -> None:
        """Dispatch to the appropriate handler based on event type."""
        etype = event.event_type
        etype_key = etype.value if hasattr(etype, "value") else str(etype)

        if etype_key == "file_written":
            self._handle_file_written(event)
        elif etype_key == "tool_call_completed":
            self._handle_tool_call_completed(event)

    # ------------------------------------------------------------------
    # Per-event-type handlers
    # ------------------------------------------------------------------

    def _handle_file_written(self, event: BaseEvent) -> None:
        """Track file metrics from file_written events."""
        path = getattr(event, "path", "")
        bytes_written = getattr(event, "bytes_written", 0)

        self._total_bytes_written += bytes_written

        if bytes_written > self._largest_file_bytes:
            self._largest_file_bytes = bytes_written

        # Function length violation heuristic: large files are more likely to
        # contain overly long functions.
        if bytes_written >= self._FUNCTION_LENGTH_THRESHOLD_BYTES:
            self._function_length_violations += 1

        # Categorize as source or test file
        if self._TEST_FILE_PATTERN.search(path):
            self._test_files_written += 1
        else:
            self._source_files_written += 1

    def _handle_tool_call_completed(self, event: BaseEvent) -> None:
        """Track lint errors from failed lint/formatter tool calls."""
        tool_name = getattr(event, "tool_name", "")
        success = getattr(event, "success", False)

        if tool_name in self._LINT_TOOLS and not success:
            self._lint_errors += 1
