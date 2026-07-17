"""OfficialSdkMcpClient — MCP client backed by the `mcp` SDK.

Supports two transport modes:
  - stdio: spawns a subprocess via ``mcp.client.stdio.stdio_client``
  - streamable_http: connects via ``mcp.client.streamable_http.streamablehttp_client``
"""

from __future__ import annotations

import logging
from typing import Any

from synapse.protocols.mcp import McpServerConfig

logger = logging.getLogger(__name__)


class OfficialSdkMcpClient:
    """Implements the :class:`~synapse.protocols.mcp.McpClient` Protocol using
    the official ``mcp`` Python SDK.

    Usage::

        client = OfficialSdkMcpClient()
        await client.connect(McpServerConfig(
            name="filesystem",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        ))
        tools = await client.list_tools()
        result = await client.call_tool("read_file", {"path": "/tmp/hello.txt"})
        await client.disconnect()
    """

    def __init__(self) -> None:
        self._connected: bool = False
        self._config: McpServerConfig | None = None

        # Transport-level context manager (e.g. return value of stdio_client())
        self._transport_ctx: Any = None

        # Read / write streams from the transport
        self._read_stream: Any = None
        self._write_stream: Any = None

        # streamable_http returns an extra callable
        self._get_session_id: Any = None

        # MCP ClientSession
        self._session: Any = None

    # ------------------------------------------------------------------
    # McpClient Protocol
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Whether the client is currently connected to an MCP server."""
        return self._connected

    async def connect(self, config: McpServerConfig) -> None:
        """Establish a connection to an MCP server.

        Selects the transport implementation based on ``config.transport``:

        * ``"stdio"`` -- calls :func:`mcp.client.stdio.stdio_client`
        * ``"streamable_http"`` -- calls :func:`mcp.client.streamable_http.streamablehttp_client`
        """
        if self._connected:
            await self.disconnect()

        if config.transport == "stdio":
            await self._connect_stdio(config)
        elif config.transport == "streamable_http":
            await self._connect_streamable_http(config)
        else:
            raise ValueError(
                f"Unknown transport type: {config.transport!r}. "
                f"Expected 'stdio' or 'streamable_http'."
            )

        # After transport is established, initialise the MCP session.
        self._session = ClientSession(self._read_stream, self._write_stream)
        await self._session.initialize()
        self._connected = True
        self._config = config

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return the list of tools exposed by the MCP server.

        Each tool is represented as a dictionary (JSON-Schema-like).
        """
        self._require_connected()
        result = await self._session.list_tools()
        return [tool.model_dump() for tool in result.tools]

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Invoke a tool on the MCP server and return the result as a dict."""
        self._require_connected()
        result = await self._session.call_tool(name, args)
        return result.model_dump()

    async def disconnect(self) -> None:
        """Close the connection to the MCP server.

        Idempotent -- safe to call multiple times.
        """
        if self._transport_ctx is not None:
            try:
                await self._transport_ctx.__aexit__(None, None, None)
            except Exception:
                logger.exception("Error while closing MCP transport")
            self._transport_ctx = None

        self._session = None
        self._read_stream = None
        self._write_stream = None
        self._get_session_id = None
        self._connected = False
        self._config = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("Not connected to an MCP server. Call connect() first.")

    async def _connect_stdio(self, config: McpServerConfig) -> None:
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=config.command,
            args=list(config.args) if config.args else [],
            env=config.env,
        )
        self._transport_ctx = stdio_client(params)
        self._read_stream, self._write_stream = await self._transport_ctx.__aenter__()

    async def _connect_streamable_http(self, config: McpServerConfig) -> None:
        from mcp.client.streamable_http import streamablehttp_client

        self._transport_ctx = streamablehttp_client(
            url=config.url,
            timeout=config.timeout,
        )
        (
            self._read_stream,
            self._write_stream,
            self._get_session_id,
        ) = await self._transport_ctx.__aenter__()


# ---------------------------------------------------------------------------
# Late import so that the module can be imported even when `mcp` is not
# installed (useful for type-checking environments).  At runtime an
# ImportError is raised inside connect() if the package is missing.
# ---------------------------------------------------------------------------
try:
    from mcp import ClientSession
except ImportError:  # pragma: no cover
    ClientSession = None  # type: ignore[assignment]
