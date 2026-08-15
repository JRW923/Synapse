"""Tool Protocol and ToolRegistry Protocol."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol


class RiskLevel(str, Enum):
    READ_ONLY = "read_only"
    WRITE_LOCAL = "write_local"
    EXECUTE = "execute"
    EXTERNAL = "external"
    META = "meta"


class ToolCategory(str, Enum):
    FILE = "file"
    CODE_UNDERSTANDING = "code_understanding"
    EXECUTION = "execution"
    INTEGRATION = "integration"
    EVAL = "eval"


@dataclass
class ToolSchema:
    name: str
    description: str
    parameters: dict  # JSON Schema for function calling


@dataclass
class ToolCallMetadata:
    tool_name: str
    start_time: datetime = field(default_factory=datetime.now)
    duration_ms: int = 0
    sandbox_used: bool = False
    sandbox_violation: bool = False
    files_touched: list[str] = field(default_factory=list)


@dataclass
class ToolResult:
    success: bool
    output: str
    error: str | None = None
    metadata: ToolCallMetadata = field(default_factory=lambda: ToolCallMetadata(tool_name=""))


class Tool(Protocol):
    """A single tool executable by the agent."""

    name: str
    description: str
    parameters: ToolSchema
    requires_sandbox: bool
    risk_level: RiskLevel
    category: ToolCategory

    async def execute(self, params: dict, sandbox=None) -> ToolResult: ...


class ToolRegistry(Protocol):
    """Registry of available tools."""

    def register(self, tool: Tool) -> None: ...
    def unregister(self, name: str) -> None: ...
    def get(self, name: str) -> Tool: ...
    def list_all(self) -> list[Tool]: ...
    def list_by_category(self, category: ToolCategory) -> list[Tool]: ...
    def get_schemas(self) -> list[dict]: ...
