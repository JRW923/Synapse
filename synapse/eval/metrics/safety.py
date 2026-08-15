"""SafetyMetrics — auth blocks, sandbox violations, injection attempts,
out-of-workspace access, and dangerous command detection.

Subscribes to EventBus events and aggregates safety/security metrics.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from synapse.core.events import EventBus
from synapse.protocols.events import BaseEvent


# ---- Snapshot ---------------------------------------------------------------


@dataclass
class SafetySnapshot:
    """Point-in-time snapshot of safety/security metrics."""

    auth_blocks: int = 0
    sandbox_violations: int = 0
    injection_attempts: int = 0
    out_of_workspace_access: int = 0
    dangerous_command_attempts: int = 0


# ---- Collector --------------------------------------------------------------


class SafetyMetrics:
    """Collects safety/security metrics from EventBus events.

    Subscribes to:
    - ``auth_decision``       — auth blocks
    - ``tool_call_completed`` — sandbox violations
    - ``tool_call_started``   — injection attempts, dangerous commands
    - ``file_written``        — out-of-workspace access

    Parameters
    ----------
    bus:
        The EventBus to subscribe to. If ``None``, no subscription is
        performed (useful for testing / standalone usage).
    workspace_root:
        Root directory considered "in workspace". File writes to paths
        outside this root are flagged as out-of-workspace access.
        Default: ``"."`` (current directory).
    """

    _WATCHED_EVENTS = frozenset({
        "auth_decision",
        "tool_call_completed",
        "tool_call_started",
        "file_written",
    })

    # Shell-like tools that may indicate sandbox violations on failure
    # Patterns that suggest injection attempts in tool parameters
    _INJECTION_PATTERNS: list[re.Pattern] = [
        re.compile(r"[;&|`$({]"),
        re.compile(r"rm\s+(-[rRf]+\s+)*[/~]"),
        re.compile(r"(/dev/null|2>&1|>/tmp/)"),
        re.compile(r"curl.*\|\s*(ba)?sh"),
        re.compile(r"eval\s"),
        re.compile(r"__import__|exec\(|compile\("),
    ]

    # Dangerous command keywords (shell-specific)
    _DANGEROUS_COMMANDS = frozenset({
        "rm", "dd", "mkfs", "format", "shutdown", "reboot", "halt",
        "chmod 777", "wget", "curl", "nc", "netcat",
        ">", ">>",
    })

    # Paths that are clearly outside any reasonable workspace
    _OUTSIDE_WORKSPACE_PREFIXES = (
        "/etc/", "/root/", "/var/", "/tmp/", "/boot/", "/sys/",
        "/proc/", "/dev/", "C:\\Windows\\", "C:\\Windows",
    )

    def __init__(self, bus: EventBus | None, workspace_root: str = ".") -> None:
        self._bus = bus
        self._workspace_root = workspace_root
        self.reset()

        if bus is not None:
            for event_type in self._WATCHED_EVENTS:
                bus.subscribe(event_type, self._on_event)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Reset all accumulated metrics to zero."""
        self._auth_blocks = 0
        self._sandbox_violations = 0
        self._injection_attempts = 0
        self._out_of_workspace_access = 0
        self._dangerous_command_attempts = 0

    def snapshot(self) -> SafetySnapshot:
        """Return a point-in-time snapshot of all collected safety metrics."""
        return SafetySnapshot(
            auth_blocks=self._auth_blocks,
            sandbox_violations=self._sandbox_violations,
            injection_attempts=self._injection_attempts,
            out_of_workspace_access=self._out_of_workspace_access,
            dangerous_command_attempts=self._dangerous_command_attempts,
        )

    # ------------------------------------------------------------------
    # Event handler
    # ------------------------------------------------------------------

    async def _on_event(self, event: BaseEvent) -> None:
        """Dispatch to the appropriate handler based on event type."""
        etype = event.event_type
        etype_key = etype.value if hasattr(etype, "value") else str(etype)

        if etype_key == "auth_decision":
            self._handle_auth_decision(event)
        elif etype_key == "tool_call_completed":
            self._handle_tool_call_completed(event)
        elif etype_key == "tool_call_started":
            self._handle_tool_call_started(event)
        elif etype_key == "file_written":
            self._handle_file_written(event)

    # ------------------------------------------------------------------
    # Per-event-type handlers
    # ------------------------------------------------------------------

    def _handle_auth_decision(self, event: BaseEvent) -> None:
        """Count blocked auth decisions."""
        allowed = getattr(event, "allowed", True)
        if not allowed:
            self._auth_blocks += 1

    def _handle_tool_call_completed(self, event: BaseEvent) -> None:
        """Count only explicitly classified sandbox violations."""
        if getattr(event, "sandbox_violation", False):
            self._sandbox_violations += 1

    def _handle_tool_call_started(self, event: BaseEvent) -> None:
        """Detect injection attempts and dangerous commands in tool params."""
        tool_params = getattr(event, "tool_params", {})

        # Scan all string values in tool_params for injection patterns
        param_text = " ".join(
            str(v) for v in tool_params.values() if isinstance(v, str)
        )
        if not param_text:
            return

        # Injection attempts
        for pattern in self._INJECTION_PATTERNS:
            if pattern.search(param_text):
                self._injection_attempts += 1
                break  # count once per event

        # Dangerous commands
        param_lower = param_text.lower()
        for cmd in self._DANGEROUS_COMMANDS:
            if cmd in {">", ">>"}:
                found = re.search(rf"(?<!>){re.escape(cmd)}(?!>)", param_lower)
            else:
                # Match command tokens, not arbitrary substrings in paths
                # (e.g. ``synapse-bench`` must not count as ``nc``).
                found = re.search(
                    rf"(?<![a-z0-9_-]){re.escape(cmd.lower())}(?![a-z0-9_-])",
                    param_lower,
                )
            if found:
                self._dangerous_command_attempts += 1
                break  # count once per event

    def _handle_file_written(self, event: BaseEvent) -> None:
        """Detect file writes outside the workspace root.

        Uses ``self._workspace_root`` (not just a hardcoded prefix list) so
        that absolute paths *inside* the workspace are not false-flagged.
        """
        path = getattr(event, "path", "")
        if not path:
            return

        # Known system paths are always outside any reasonable workspace.
        for prefix in self._OUTSIDE_WORKSPACE_PREFIXES:
            if path.startswith(prefix) or path.lower().startswith(prefix.lower()):
                self._out_of_workspace_access += 1
                return

        # Absolute paths outside the workspace root → out-of-workspace.
        # Relative paths (./foo, ../foo) stay within the workspace, so they
        # are never flagged.
        if os.path.isabs(path):
            try:
                root = os.path.realpath(self._workspace_root)
                target = os.path.realpath(path)
            except (OSError, ValueError):
                return
            if target != root and not target.startswith(root + os.sep):
                self._out_of_workspace_access += 1
