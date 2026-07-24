"""Tests for the process-quality verification closed loop (TODO B).

Covers: sequence pattern recognition (grep-before-write vs direct write),
score/hint generation, event emission, memory persistence, and retriever
injection of the feedback into the next task's context.
"""

from __future__ import annotations

import pytest

from synapse.core.events import EventBus
from synapse.protocols.events import (
    ProcessQualityScored,
    ToolCallCompleted,
    ToolCallStarted,
)
from synapse.protocols.memory import MemoryEntry, MemoryLevel, MemoryMetadata
from synapse.modules.process_quality import ProcessQualityVerifier
from synapse.modules.context.retriever import BasicContextRetriever


class FakeMemory:
    """In-memory MemoryStore fake recording store/forget/retrieve."""

    def __init__(self, project_entries=None):
        self.stored = []
        self.forgotten = []
        self._project = list(project_entries or [])

    async def store(self, entry):
        self.stored.append(entry)

    async def forget(self, entry_id):
        self.forgotten.append(entry_id)

    async def retrieve(self, query, level, top_k=5):
        if level == MemoryLevel.PROJECT and self._project:
            return list(self._project[:top_k])
        return []


def _run_verifier(events, success=True):
    """Drive a verifier through *events* and return its report + emitted."""
    import asyncio

    bus = EventBus()
    emitted = []

    async def _capture(e):
        emitted.append(e)

    bus.subscribe("process_quality_scored", _capture)
    memory = FakeMemory()
    v = ProcessQualityVerifier(event_bus=bus, memory=memory)

    async def go():
        for name, kwargs in events:
            if name == "start":
                await bus.emit(ToolCallStarted(session_id="s", **kwargs))
            else:
                await bus.emit(ToolCallCompleted(session_id="s", **kwargs))
        report = await v.after_task("do the thing", success=success, session_id="s")
        return report

    report = asyncio.run(go())
    return report, emitted, memory


def test_reuse_detected_when_grep_before_write():
    events = [
        ("start", {"tool_name": "grep", "tool_params": {"pattern": "foo", "path": "src"}}),
        ("done", {"tool_name": "grep", "success": True, "duration_ms": 1, "files_touched": []}),
        ("start", {"tool_name": "write", "tool_params": {"path": "src/foo.py"}}),
        ("done", {"tool_name": "write", "success": True, "duration_ms": 1, "files_touched": ["src/foo.py"]}),
    ]
    report, emitted, memory = _run_verifier(events, success=True)

    assert report.reuse_ratio == 1.0
    assert report.write_without_lookup == 0
    assert report.success is True
    assert report.score >= 0.9

    # emitted + persisted
    assert len(emitted) == 1
    assert isinstance(emitted[0], ProcessQualityScored)
    assert emitted[0].score == report.score
    assert memory.stored and memory.stored[0].level == MemoryLevel.PROJECT
    assert "Process Quality Feedback" in memory.stored[0].content


def test_write_without_lookup_lowers_score():
    events = [
        ("start", {"tool_name": "write", "tool_params": {"path": "src/foo.py"}}),
        ("done", {"tool_name": "write", "success": True, "duration_ms": 1, "files_touched": ["src/foo.py"]}),
    ]
    report, emitted, _ = _run_verifier(events, success=True)

    assert report.reuse_ratio == 0.0
    assert report.write_without_lookup == 1
    # success alone (0.4) without reuse → well below a reuse-positive run
    assert report.score < 0.6
    assert "先 grep/read" in report.hint

    assert emitted and emitted[0].write_without_lookup == 1


def test_failed_task_penalizes_score():
    events = [
        ("start", {"tool_name": "grep", "tool_params": {"pattern": "x", "path": "src"}}),
        ("done", {"tool_name": "grep", "success": True, "duration_ms": 1, "files_touched": []}),
        ("start", {"tool_name": "write", "tool_params": {"path": "src/foo.py"}}),
        ("done", {"tool_name": "write", "success": True, "duration_ms": 1, "files_touched": ["src/foo.py"]}),
    ]
    report, _, _ = _run_verifier(events, success=False)
    # reuse is good, but failure caps the score below a perfect run
    assert report.reuse_ratio == 1.0
    assert 0.4 < report.score < 1.0


def test_retriever_injects_feedback_into_next_prompt():
    feedback = MemoryEntry(
        id="process_quality_feedback",
        content="# Process Quality Feedback\n\nScore: 0.40 · Reuse: 0%\n\n上次任务有 1 次直接写文件而未见先检索现有实现。\n",
        level=MemoryLevel.PROJECT,
        metadata=MemoryMetadata(tags=["process_quality", "feedback"]),
    )
    memory = FakeMemory(project_entries=[feedback])
    retriever = BasicContextRetriever()

    import asyncio
    blocks = asyncio.run(retriever._build_reference("add a feature", memory))

    injected = [b for b in blocks if "Process Quality Feedback" in b.content]
    assert injected, "feedback should be injected into reference context"
    assert injected[0].source.value == "memory"
