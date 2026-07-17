"""MCP Client Protocol and Server Config."""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class McpServerConfig:
    """Configuration for connecting to an MCP server."""

    name: str
    transport: str = "stdio"           # "stdio" | "streamable_http"
    command: str | None = None         # stdio: executable path
    args: list[str] | None = None      # stdio: command-line arguments
    url: str | None = None             # streamable_http: HTTP URL
    env: dict[str, str] | None = None  # stdio: environment variables
    timeout: int = 30


class McpClient(Protocol):
    """Protocol for MCP client implementations.

    Implementations handle transport-level details (stdio subprocess,
    streamable HTTP) and expose a uniform async interface for tool
    discovery and invocation.
    """

    async def connect(self, config: McpServerConfig) -> None:
        """Establish connection to the MCP server."""
        ...

    async def list_tools(self) -> list[dict]:
        """Return available tools as a list of JSON-Schema-like dicts."""
        ...

    async def call_tool(self, name: str, args: dict) -> dict:
        """Invoke a tool by name with the given arguments."""
        ...

    async def disconnect(self) -> None:
        """Close the connection to the MCP server."""
        ...

    @property
    def connected(self) -> bool:
        """Whether the client is currently connected."""
        ...
