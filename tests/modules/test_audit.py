"""Tests for AuditLogger — immutable audit log with HMAC signatures."""
import json
import hmac
import hashlib
import tempfile
from pathlib import Path

import pytest

from synapse.core.events import EventBus
from synapse.protocols.events import (
    ToolCallStarted,
    ToolCallCompleted,
    AuthDecisionMade,
)


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def tmp_audit_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


# ---------------------------------------------------------------------------
# Test 1: tool_call events are logged to JSONL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_logs_tool_call_events(bus, tmp_audit_dir):
    from synapse.modules.security.audit import AuditLogger

    logger = AuditLogger(bus=bus, audit_dir=str(tmp_audit_dir))

    await bus.emit(ToolCallStarted(session_id="s1", tool_name="read", tool_params={"path": "f.py"}))
    await bus.emit(ToolCallCompleted(
        session_id="s1", tool_name="read", success=True, duration_ms=42, files_touched=["f.py"],
    ))
    await bus.emit(AuthDecisionMade(
        session_id="s1", tool_name="read", allowed=True, reason="safe",
    ))

    # Check that a JSONL file was created for today
    audit_files = list(tmp_audit_dir.glob("*.jsonl"))
    assert len(audit_files) == 1

    lines = audit_files[0].read_text().strip().split("\n")
    assert len(lines) == 3

    entries = [json.loads(line) for line in lines]
    event_types = [e["event_type"] for e in entries]
    assert "tool_call_started" in event_types
    assert "tool_call_completed" in event_types
    assert "auth_decision" in event_types

    # Every entry must have a signature
    for entry in entries:
        assert "signature" in entry
        assert len(entry["signature"]) == 64  # SHA-256 hex digest


# ---------------------------------------------------------------------------
# Test 2: HMAC tamper detection
# ---------------------------------------------------------------------------

def test_hmac_verification(tmp_audit_dir):
    from synapse.modules.security.audit import AuditLogger, AuditEntry

    key = b"test-secret-key"
    logger = AuditLogger(bus=None, audit_dir=str(tmp_audit_dir), session_key=key)

    # Build an entry manually and sign it
    entry = AuditEntry(
        event_id="ev-1",
        timestamp="2025-01-01T00:00:00",
        event_type="tool_call_started",
        session_id="s1",
        data={"tool_name": "read"},
    )

    # Sign the entry
    signed = logger._sign_entry(entry)
    assert signed["signature"]
    assert logger._verify_entry(signed)

    # Tamper with the data — verification must fail
    signed["data"]["tool_name"] = "tampered"
    assert not logger._verify_entry(signed)

    # Tamper with the event_type
    signed2 = logger._sign_entry(entry)
    signed2["event_type"] = "auth_decision"
    assert not logger._verify_entry(signed2)

    # An entry with no signature at all must fail
    assert not logger._verify_entry({
        "event_id": "ev-1",
        "timestamp": "2025-01-01T00:00:00",
        "event_type": "tool_call_started",
        "session_id": "s1",
        "data": {},
    })

    # An entry with a wrong-length signature must fail
    assert not logger._verify_entry({
        "event_id": "ev-1",
        "timestamp": "2025-01-01T00:00:00",
        "event_type": "tool_call_started",
        "session_id": "s1",
        "data": {},
        "signature": "bad",
    })


# ---------------------------------------------------------------------------
# Test 3: export and query by session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_export_and_query(bus, tmp_audit_dir):
    from synapse.modules.security.audit import AuditLogger

    logger = AuditLogger(bus=bus, audit_dir=str(tmp_audit_dir))

    # Emit events for two different sessions
    await bus.emit(ToolCallStarted(session_id="s1", tool_name="read", tool_params={}))
    await bus.emit(ToolCallStarted(session_id="s2", tool_name="write", tool_params={}))
    await bus.emit(ToolCallCompleted(session_id="s1", tool_name="read", success=True, duration_ms=10, files_touched=[]))
    await bus.emit(ToolCallCompleted(session_id="s2", tool_name="write", success=False, duration_ms=20, files_touched=[]))

    # export returns all entries
    all_entries = logger.export()
    assert len(all_entries) == 4

    # query filters by session_id
    s1_entries = logger.query(session_id="s1")
    assert len(s1_entries) == 2
    assert all(e["session_id"] == "s1" for e in s1_entries)

    s2_entries = logger.query(session_id="s2")
    assert len(s2_entries) == 2
    assert all(e["session_id"] == "s2" for e in s2_entries)

    # query non-existent session returns empty list
    assert logger.query(session_id="nonexistent") == []
