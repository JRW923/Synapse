"""ProcessMetrics — reuse stats, root-cause accuracy, test persistence,
instruction drift, plan/merge quality, and thrashing counts.

Subscribes to EventBus events and aggregates cross-cutting metrics about
the development process itself (not the code quality).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from synapse.core.events import EventBus
from synapse.protocols.events import BaseEvent


# ---- Snapshot ---------------------------------------------------------------


@dataclass
class ProcessSnapshot:
    """Point-in-time snapshot of process-level metrics."""

    reuse_attempted: int = 0
    reuse_found: int = 0
    reuse_adopted: int = 0
    reuse_ratio: float = 0.0
    write_without_lookup: int = 0
    process_score: float = 0.0

    root_cause_accuracy: float = 0.0

    tests_persisted: int = 0
    total_files_written: int = 0
    test_persistence_rate: float = 0.0

    instruction_drift_at_round: int = 0

    plan_quality_score: float = 0.0
    merge_quality_score: float = 0.0

    thrashing_events: int = 0
    regex_abuse_events: int = 0


# ---- Collector --------------------------------------------------------------


class ProcessMetrics:
    """Collects process-level metrics from EventBus events.

    Subscribes to:
    - ``tool_call_started``  — reuse attempts
    - ``tool_call_completed`` — reuse found/adopted, instruction drift
    - ``file_written``       — test persistence rate
    - ``agent_completed``    — root-cause accuracy
    - ``plan_created``       — plan quality score
    - ``merge_result``       — merge quality score
    - ``thrashing_detected`` — thrashing / regex abuse counts

    Parameters
    ----------
    bus:
        The EventBus to subscribe to. If ``None``, no subscription is
        performed (useful for testing / standalone usage).
    """

    _WATCHED_EVENTS = frozenset({
        "tool_call_started",
        "tool_call_completed",
        "file_written",
        "agent_completed",
        "plan_created",
        "merge_result",
        "thrashing_detected",
        "process_quality_scored",
    })

    # Tools whose names suggest a reuse operation
    _REUSE_TOOLS = frozenset({
        "find_reuse", "adopt_reuse", "check_reuse", "apply_reuse",
        "suggest_reuse", "reuse",
    })

    # Files considered as test files for persistence tracking
    _TEST_FILE_PATTERN = re.compile(
        r"(^|[\\/])(tests?[\\/]|test_|spec_|_test\.|_spec\.)",
    )

    # High modification count threshold for regex-abuse classification
    _REGEX_ABUSE_THRESHOLD = 5

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
        self._reuse_attempted = 0
        self._reuse_found = 0
        self._reuse_adopted = 0
        self._reuse_ratio = 0.0
        self._write_without_lookup = 0
        self._process_score = 0.0

        self._agent_successes = 0
        self._agent_completions = 0

        self._tests_persisted = 0
        self._total_files_written = 0

        self._instruction_drift_count = 0

        self._plan_scores: list[float] = []
        self._merge_scores: list[float] = []

        self._thrashing_events = 0
        self._regex_abuse_events = 0

    def snapshot(self) -> ProcessSnapshot:
        """Return a point-in-time snapshot of all collected metrics."""
        persistence_rate = 0.0
        if self._total_files_written > 0:
            persistence_rate = self._tests_persisted / self._total_files_written

        root_cause_accuracy = 0.0
        if self._agent_completions > 0:
            root_cause_accuracy = self._agent_successes / self._agent_completions

        plan_quality = 0.0
        if self._plan_scores:
            plan_quality = sum(self._plan_scores) / len(self._plan_scores)

        merge_quality = 0.0
        if self._merge_scores:
            merge_quality = sum(self._merge_scores) / len(self._merge_scores)

        return ProcessSnapshot(
            reuse_attempted=self._reuse_attempted,
            reuse_found=self._reuse_found,
            reuse_adopted=self._reuse_adopted,
            reuse_ratio=round(self._reuse_ratio, 4),
            write_without_lookup=self._write_without_lookup,
            process_score=round(self._process_score, 4),

            root_cause_accuracy=round(root_cause_accuracy, 4),

            tests_persisted=self._tests_persisted,
            total_files_written=self._total_files_written,
            test_persistence_rate=round(persistence_rate, 4),

            instruction_drift_at_round=self._instruction_drift_count,

            plan_quality_score=round(plan_quality, 4),
            merge_quality_score=round(merge_quality, 4),

            thrashing_events=self._thrashing_events,
            regex_abuse_events=self._regex_abuse_events,
        )

    # ------------------------------------------------------------------
    # Event handler
    # ------------------------------------------------------------------

    async def _on_event(self, event: BaseEvent) -> None:
        """Dispatch to the appropriate handler based on event type."""
        etype = event.event_type
        if isinstance(etype, str):
            etype_key = etype
        else:
            etype_key = etype.value

        if etype_key == "tool_call_started":
            self._handle_tool_call_started(event)
        elif etype_key == "tool_call_completed":
            self._handle_tool_call_completed(event)
        elif etype_key == "file_written":
            self._handle_file_written(event)
        elif etype_key == "agent_completed":
            self._handle_agent_completed(event)
        elif etype_key == "plan_created":
            self._handle_plan_created(event)
        elif etype_key == "merge_result":
            self._handle_merge_result(event)
        elif etype_key == "thrashing_detected":
            self._handle_thrashing_detected(event)
        elif etype_key == "process_quality_scored":
            self._handle_process_quality_scored(event)

    # ------------------------------------------------------------------
    # Per-event-type handlers
    # ------------------------------------------------------------------

    def _handle_tool_call_started(self, event: BaseEvent) -> None:
        """Track reuse attempts from tool_call_started events."""
        tool_name = getattr(event, "tool_name", "")
        if tool_name in self._REUSE_TOOLS:
            self._reuse_attempted += 1

    def _handle_tool_call_completed(self, event: BaseEvent) -> None:
        """Track reuse found/adopted and instruction drift per round."""
        tool_name = getattr(event, "tool_name", "")
        success = getattr(event, "success", False)

        # Reuse found: a find_reuse call that succeeded
        if tool_name == "find_reuse" and success:
            self._reuse_found += 1

        # Reuse adopted: an adopt_reuse call that succeeded
        if tool_name == "adopt_reuse" and success:
            self._reuse_adopted += 1

        # Instruction drift: each completed call counts as one round of drift
        # observation (heuristic — real systems would compare parameters to
        # original instructions)
        self._instruction_drift_count += 1

    def _handle_file_written(self, event: BaseEvent) -> None:
        """Track test persistence from file_written events."""
        self._total_files_written += 1

        path = getattr(event, "path", "")
        if self._TEST_FILE_PATTERN.search(path):
            self._tests_persisted += 1

    def _handle_agent_completed(self, event: BaseEvent) -> None:
        """Track root-cause accuracy from agent_completed events."""
        status = getattr(event, "status", "")
        self._agent_completions += 1
        if status == "success":
            self._agent_successes += 1

    def _handle_plan_created(self, event: BaseEvent) -> None:
        """Score plan quality based on number of steps and reasoning."""
        plan_steps = getattr(event, "plan_steps", [])
        reasoning = getattr(event, "reasoning", "")

        # Score: step count contributes, reasoning presence contributes
        step_score = min(len(plan_steps), 10) / 10.0     # cap at 10 steps
        reasoning_score = 1.0 if len(reasoning) > 20 else (len(reasoning) / 20.0)
        quality = (step_score * 0.6) + (reasoning_score * 0.4)
        self._plan_scores.append(round(quality, 4))

    def _handle_merge_result(self, event: BaseEvent) -> None:
        """Score merge quality based on subtask count and output length."""
        subtask_count = getattr(event, "subtask_count", 0)
        merged_output = getattr(event, "merged_output", "")

        # Score: higher subtask count suggests better decomposition;
        # non-empty merged output indicates successful merge
        count_score = min(subtask_count, 10) / 10.0
        output_score = 1.0 if len(merged_output) > 20 else (len(merged_output) / 20.0)
        quality = (count_score * 0.5) + (output_score * 0.5)
        self._merge_scores.append(round(quality, 4))

    def _handle_thrashing_detected(self, event: BaseEvent) -> None:
        """Track thrashing and regex abuse from thrashing_detected events."""
        self._thrashing_events += 1

        modification_count = getattr(event, "modification_count", 0)
        if modification_count >= self._REGEX_ABUSE_THRESHOLD:
            self._regex_abuse_events += 1

    def _handle_process_quality_scored(self, event: BaseEvent) -> None:
        """Copy the real process verifier result into the run snapshot."""
        self._reuse_ratio = float(getattr(event, "reuse_ratio", 0.0) or 0.0)
        self._write_without_lookup = int(getattr(event, "write_without_lookup", 0) or 0)
        self._process_score = float(getattr(event, "score", 0.0) or 0.0)
