"""Immutable audit log with HMAC-signed JSONL entries.

Subscribes to EventBus events (tool_call_started, tool_call_completed,
auth_decision) and writes tamper-evident entries to daily JSONL files.
"""

from __future__ import annotations

import asyncio
import hmac
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from synapse.core.events import EventBus
from synapse.protocols.events import BaseEvent


@dataclass
class AuditEntry:
    """A single auditable event, serialised as one JSONL line."""

    event_id: str
    timestamp: str
    event_type: str
    session_id: str
    data: dict[str, Any] = field(default_factory=dict)
    signature: str = ""

    @classmethod
    def from_event(cls, event: BaseEvent) -> "AuditEntry":
        """Create an AuditEntry from a BaseEvent, extracting the payload."""
        data: dict[str, Any] = {}
        for f_name, f_val in event.__dict__.items():
            if f_name in ("event_id", "timestamp", "event_type", "session_id", "signature"):
                continue
            if isinstance(f_val, datetime):
                data[f_name] = f_val.isoformat()
            else:
                data[f_name] = f_val
        return cls(
            event_id=event.event_id,
            timestamp=event.timestamp.isoformat(),
            event_type=event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type),
            session_id=event.session_id,
            data=data,
        )


class AuditLogger:
    """Immutable audit log.

    Subscribes to the EventBus and writes every matched event as a
    signed JSONL line to ``.synapse/audit/YYYY-MM-DD.jsonl``.

    Parameters
    ----------
    bus:
        The EventBus to subscribe to.  If ``None``, no subscription is
        performed (useful for testing / standalone usage).
    audit_dir:
        Directory in which daily JSONL files are stored.
    session_key:
        Secret bytes used for HMAC-SHA256 signing.  A random 32-byte key
        is generated when not provided.
    """

    _WATCHED_EVENTS = frozenset({
        "tool_call_started",
        "tool_call_completed",
        "auth_decision",
    })

    def __init__(
        self,
        bus: EventBus | None,
        audit_dir: str = ".synapse/audit",
        session_key: bytes | None = None,
    ) -> None:
        self._bus = bus
        self._audit_dir = Path(audit_dir)
        self._session_key = session_key or secrets.token_bytes(32)
        self._write_lock = asyncio.Lock()

        # Ensure the audit directory exists
        self._audit_dir.mkdir(parents=True, exist_ok=True)

        # Subscribe to relevant events
        if bus is not None:
            for event_type in self._WATCHED_EVENTS:
                bus.subscribe(event_type, self._on_event)

    # ------------------------------------------------------------------
    # Event handler
    # ------------------------------------------------------------------

    async def _on_event(self, event: BaseEvent) -> None:
        """Callback for EventBus — writes a signed entry to the daily file."""
        entry = AuditEntry.from_event(event)
        signed = self._sign_entry(entry)
        line = json.dumps(signed, ensure_ascii=False, sort_keys=True) + "\n"

        filepath = self._daily_path()

        async with self._write_lock:
            with open(filepath, "a", encoding="utf-8") as fh:
                fh.write(line)

    # ------------------------------------------------------------------
    # Signing / verification
    # ------------------------------------------------------------------

    def _sign_entry(self, entry: AuditEntry) -> dict[str, Any]:
        """Return a dict representation of *entry* with ``signature`` set."""
        payload = {
            "event_id": entry.event_id,
            "timestamp": entry.timestamp,
            "event_type": entry.event_type,
            "session_id": entry.session_id,
            "data": entry.data,
        }
        sig = self._compute_hmac(payload)
        payload["signature"] = sig
        return payload

    def _verify_entry(self, entry: dict[str, Any]) -> bool:
        """Return ``True`` if *entry* has a valid HMAC signature."""
        try:
            sig = entry.get("signature", "")
            if not sig or len(sig) != 64:
                return False
            payload = {
                "event_id": entry["event_id"],
                "timestamp": entry["timestamp"],
                "event_type": entry["event_type"],
                "session_id": entry["session_id"],
                "data": entry["data"],
            }
            expected = self._compute_hmac(payload)
            return hmac.compare_digest(sig, expected)
        except (KeyError, TypeError):
            return False

    def _compute_hmac(self, payload: dict[str, Any]) -> str:
        """HMAC-SHA256 hex digest over the canonical JSON of *payload*."""
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hmac.new(self._session_key, raw, hashlib.sha256).hexdigest()

    # ------------------------------------------------------------------
    # Export / query
    # ------------------------------------------------------------------

    def export(self, format: str = "jsonl") -> list[dict[str, Any]]:
        """Return all entries from every JSONL file in the audit directory.

        Only ``"jsonl"`` format is supported.
        """
        if format != "jsonl":
            raise ValueError(f"Unsupported export format: {format!r}")

        entries: list[dict[str, Any]] = []
        for filepath in sorted(self._audit_dir.glob("*.jsonl")):
            with open(filepath, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return entries

    def query(self, session_id: str) -> list[dict[str, Any]]:
        """Return all entries belonging to *session_id*."""
        return [e for e in self.export() if e.get("session_id") == session_id]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _daily_path(self) -> Path:
        """Return the path for today's JSONL file."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._audit_dir / f"{today}.jsonl"
