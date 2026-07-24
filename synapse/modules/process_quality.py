"""Process Quality Verification Closed Loop (TODO B).

After each task, verify **how** the agent worked — not just whether the patch
passed.  During the run we capture the ordered tool-call sequence from the
EventBus; on task completion we score two things:

* **reuse behaviour** — did the agent look before it wrote?  A ``write``/``edit``
  that is preceded in the sequence by a ``read``/``grep``/``glob`` touching the
  same file is "reuse-positive"; a write with no preceding lookup is a
  "write-without-lookup".
* **outcome** — did the task succeed?

The composite score and a natural-language hint are (1) emitted on the
EventBus as ``ProcessQualityScored`` and (2) stored to PROJECT memory as a
single rolling entry, which the context retriever injects back into the next
task's prompt (see ``BasicContextRetriever._build_reference``).

ponytail: relating a lookup to a write is a naive path substring / ancestor
check — not AST- or symbol-aware.  Good enough to nudge "search before write";
upgrade path is to resolve both sides to a file symbol via the AST retriever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from synapse.core.events import EventBus
from synapse.protocols.events import BaseEvent, ProcessQualityScored
from synapse.protocols.memory import MemoryEntry, MemoryLevel, MemoryMetadata


# Tools that inspect existing code/files — a "lookup" before writing.
_LOOKUP_TOOLS = frozenset({"read", "grep", "glob", "git"})
# Tools that create/modify files — a "write" whose preceding lookup we check.
_MUTATE_TOOLS = frozenset({"write", "edit"})

# Fixed memory id/tag so the feedback is a single rolling entry (upsert).
_FEEDBACK_ID = "process_quality_feedback"
_FEEDBACK_TAGS = ["process_quality", "feedback"]
# Query the retriever uses to pull the entry back (matches content sentinel).
_FEEDBACK_QUERY = "process quality feedback"


@dataclass
class ProcessQualityReport:
    """Result of verifying one task's process quality."""

    task: str
    score: float
    reuse_ratio: float
    write_without_lookup: int
    thrashing_events: int
    success: bool
    tool_calls: int
    hint: str
    timestamp: datetime = field(default_factory=datetime.now)


def _norm_path(p: str) -> str:
    return (p or "").replace("\\", "/").strip().lower()


def _entry_path(ev: dict) -> str:
    """File/dir path associated with a captured sequence entry."""
    files = ev.get("files") or []
    if files:
        return _norm_path(files[0])
    params = ev.get("params") or {}
    for key in ("path", "file_path", "file"):
        val = params.get(key)
        if isinstance(val, str) and val:
            return _norm_path(val)
    return ""


def _relates(write_path: str, lookup_path: str) -> bool:
    """Heuristic: does *lookup_path* plausibly cover *write_path*?"""
    if not write_path or not lookup_path:
        return False
    if write_path == lookup_path:
        return True
    # lookup is a directory that contains the written file
    if write_path.startswith(lookup_path) and (
        len(write_path) == len(lookup_path)
        or write_path[len(lookup_path)] == "/"
    ):
        return True
    # loose: one path contains the other (catches basename overlaps)
    if lookup_path in write_path or write_path in lookup_path:
        return True
    return False


class ProcessQualityVerifier:
    """Captures the tool-call sequence and verifies process quality per task.

    Subscribe to the EventBus in ``__init__``; call :meth:`after_task` from the
    agent's post-task hook to score, emit, and persist feedback.
    """

    def __init__(self, event_bus: EventBus | None = None, memory=None) -> None:
        self._bus = event_bus
        self._memory = memory
        self._reset()
        if event_bus is not None:
            event_bus.subscribe("tool_call_started", self._on_tool_started)
            event_bus.subscribe("tool_call_completed", self._on_tool_completed)
            event_bus.subscribe("thrashing_detected", self._on_thrashing)

    def _reset(self) -> None:
        self._sequence: list[dict] = []
        self._thrashing = 0

    # ------------------------------------------------------------------
    # Event capture
    # ------------------------------------------------------------------

    async def _on_tool_started(self, event: BaseEvent) -> None:
        self._sequence.append({
            "name": getattr(event, "tool_name", ""),
            "params": getattr(event, "tool_params", {}) or {},
            "completed": False,
        })

    async def _on_tool_completed(self, event: BaseEvent) -> None:
        name = getattr(event, "tool_name", "")
        for entry in reversed(self._sequence):
            if entry["name"] == name and not entry.get("completed"):
                entry.update({
                    "completed": True,
                    "success": getattr(event, "success", False),
                    "files": getattr(event, "files_touched", []) or [],
                })
                return
        self._sequence.append({
            "name": name,
            "params": {},
            "completed": True,
            "success": getattr(event, "success", False),
            "files": getattr(event, "files_touched", []) or [],
        })

    async def _on_thrashing(self, event: BaseEvent) -> None:
        self._thrashing += 1

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    def _analyze(self, success: bool) -> ProcessQualityReport:
        mutating = 0
        preceded_by_lookup = 0
        write_without_lookup = 0

        for i, ev in enumerate(self._sequence):
            if ev["name"] in _MUTATE_TOOLS and ev.get("completed", True):
                mutating += 1
                wpath = _entry_path(ev)
                had_lookup = False
                for prev in self._sequence[:i]:
                    if prev["name"] in _LOOKUP_TOOLS:
                        if _relates(wpath, _entry_path(prev)):
                            had_lookup = True
                            break
                if had_lookup:
                    preceded_by_lookup += 1
                else:
                    write_without_lookup += 1

        reuse_ratio = (preceded_by_lookup / mutating) if mutating else 1.0
        success_factor = 1.0 if success else 0.3
        # ponytail: simple weighted blend, reuse weighted highest.
        score = 0.6 * reuse_ratio + 0.4 * success_factor
        # small penalty for thrashing (capped)
        if self._thrashing:
            score = max(0.0, score - min(self._thrashing, 3) * 0.05)

        report = ProcessQualityReport(
            task="",  # filled by caller
            score=round(score, 3),
            reuse_ratio=round(reuse_ratio, 3),
            write_without_lookup=write_without_lookup,
            thrashing_events=self._thrashing,
            success=success,
            tool_calls=len(self._sequence),
            hint=self._make_hint(reuse_ratio, write_without_lookup, success, self._thrashing),
        )
        return report

    @staticmethod
    def _make_hint(
        reuse_ratio: float, writes_no_lookup: int, success: bool, thrashing: int
    ) -> str:
        if writes_no_lookup > 0 and reuse_ratio < 0.5:
            return (
                f"上次任务有 {writes_no_lookup} 次直接写/改文件而未见先检索现有实现"
                f"（复用率 {reuse_ratio:.0%}）。下次请先 grep/read 定位可复用代码，"
                f"再动手写，避免重复造轮子。"
            )
        if thrashing:
            return (
                f"上次任务出现 {thrashing} 次对同一文件的反复修改（thrashing）。"
                f"下次先读懂再改，避免来回试错。"
            )
        return (
            f"上次任务过程质量良好（复用率 {reuse_ratio:.0%}，"
            f"{'成功' if success else '未完成'}）。继续保持先检索后修改的习惯。"
        )

    # ------------------------------------------------------------------
    # Public — called from Agent.run post-task hook
    # ------------------------------------------------------------------

    async def after_task(
        self, task: str, success: bool, session_id: str = ""
    ) -> ProcessQualityReport:
        """Score the just-finished task, emit the result, persist feedback."""
        report = self._analyze(success)
        report.task = task

        if self._bus is not None:
            await self._bus.emit(ProcessQualityScored(
                session_id=session_id,
                task=task,
                score=report.score,
                reuse_ratio=report.reuse_ratio,
                write_without_lookup=report.write_without_lookup,
                thrashing_events=report.thrashing_events,
                success=report.success,
                tool_calls=report.tool_calls,
                hint=report.hint,
            ))

        if self._memory is not None:
            await self._store_feedback(report)

        self._reset()
        return report

    async def _store_feedback(self, report: ProcessQualityReport) -> None:
        content = (
            f"# Process Quality Feedback\n\n"
            f"Score: {report.score:.2f} · Reuse: {report.reuse_ratio:.0%} · "
            f"Success: {report.success}\n\n{report.hint}\n"
        )
        entry = MemoryEntry(
            id=_FEEDBACK_ID,
            content=content,
            level=MemoryLevel.PROJECT,
            metadata=MemoryMetadata(
                timestamp=report.timestamp,
                tags=_FEEDBACK_TAGS,
                source_task=report.task,
            ),
        )
        # Rolling upsert: drop any prior feedback entry first.
        try:
            await self._memory.forget(_FEEDBACK_ID)
        except Exception:
            pass
        try:
            await self._memory.store(entry)
        except Exception:
            pass
