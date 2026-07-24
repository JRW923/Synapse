"""Sandbox Protocol and authorization types."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    platform: str = ""


@dataclass
class AuthRequest:
    tool_name: str
    tool_params: dict
    risk_level: str  # RiskLevel value
    session_id: str
    user_id: str | None = None


@dataclass
class AuthDecision:
    allowed: bool
    reason: str
    requires_confirmation: bool = False


class Sandbox(Protocol):
    """Process sandbox — cross-platform execution isolation."""

    @property
    def platform(self) -> str: ...

    async def execute(
        self,
        command: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 120,
    ) -> SandboxResult: ...
