"""Action-Time Authorization — evaluates tool calls before execution."""

import os
from pathlib import Path
from synapse.protocols.tool import RiskLevel
from synapse.protocols.sandbox import AuthRequest, AuthDecision


class ActionAuthorizer:
    """Evaluates tool call authorization based on risk level, workspace, and allowlists."""

    DANGEROUS_PATTERNS = [
        "rm -rf /",
        "rm -rf --no-preserve-root",
        "dd if=/dev/zero",
        "> /dev/sda",
        "mkfs.",
        ":(){ :|:& };:",  # fork bomb
        "chmod 777 /",
        "chown -R",
    ]

    ALWAYS_ALLOWED_COMMANDS = [
        "ls", "echo", "cat", "head", "tail", "wc", "pwd", "env",
        "git", "python", "python3", "pip", "npm", "node", "cargo",
        "go", "pytest", "mypy", "ruff", "black",
    ]

    def __init__(
        self,
        workspace_root: str = ".",
        allow_external: bool = False,
        confirmation_enabled: bool = True,
    ):
        self.workspace_root = Path(workspace_root).resolve()
        self.allow_external = allow_external
        self.confirmation_enabled = confirmation_enabled

    def create_request(
        self, tool_name: str, params: dict, risk_level: RiskLevel, session_id: str, user_id: str | None = None,
    ) -> AuthRequest:
        return AuthRequest(
            tool_name=tool_name,
            tool_params=params,
            risk_level=risk_level.value if isinstance(risk_level, RiskLevel) else risk_level,
            session_id=session_id,
            user_id=user_id,
        )

    def authorize(self, request: AuthRequest) -> AuthDecision:
        risk = request.risk_level

        # READ_ONLY: always allow
        if risk == RiskLevel.READ_ONLY.value:
            return AuthDecision(allowed=True, reason="Read-only operation", requires_confirmation=False)

        # WRITE_LOCAL: allow in workspace, confirm
        if risk == RiskLevel.WRITE_LOCAL.value:
            if self._is_in_workspace(request):
                return AuthDecision(
                    allowed=True,
                    reason="Write within workspace",
                    requires_confirmation=self.confirmation_enabled,
                )
            return AuthDecision(allowed=False, reason="Write target is outside workspace")

        # EXECUTE: allowlist check + dangerous pattern check
        if risk == RiskLevel.EXECUTE.value:
            command = request.tool_params.get("command", "")
            if self._is_dangerous(command):
                return AuthDecision(allowed=False, reason="Command matches dangerous pattern")
            if not self._is_allowlisted(command):
                return AuthDecision(allowed=False, reason=f"Command not in allowlist: {command.split()[0] if command else ''}")
            return AuthDecision(
                allowed=True,
                reason="Command in allowlist",
                requires_confirmation=self.confirmation_enabled,
            )

        # EXTERNAL: must be explicitly enabled
        if risk == RiskLevel.EXTERNAL.value:
            if self.allow_external:
                return AuthDecision(allowed=True, reason="External access enabled", requires_confirmation=True)
            return AuthDecision(allowed=False, reason="External tools are disabled")

        # META: allow
        if risk == RiskLevel.META.value:
            return AuthDecision(allowed=True, reason="Meta/experimental tool")

        return AuthDecision(allowed=False, reason=f"Unknown risk level: {risk}")

    def _is_in_workspace(self, request: AuthRequest) -> bool:
        target = request.tool_params.get("path", "")
        if not target:
            return False
        try:
            resolved = Path(target).resolve()
            return str(resolved).startswith(str(self.workspace_root))
        except (ValueError, OSError):
            return False

    def _is_dangerous(self, command: str) -> bool:
        for pattern in self.DANGEROUS_PATTERNS:
            if pattern in command:
                return True
        return False

    def _is_allowlisted(self, command: str) -> bool:
        if not command.strip():
            return False
        base = command.strip().split()[0]
        # Allow commands starting with any allowlisted prefix
        return base in self.ALWAYS_ALLOWED_COMMANDS
