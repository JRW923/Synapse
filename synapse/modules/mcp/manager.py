"""McpManager — lifecycle manager for MCP server connections.

Orchestrates connecting, tool registration, and disconnecting for
multiple MCP servers.

Usage::

    manager = McpManager(tool_registry=registry, event_bus=event_bus)
    tools = await manager.add_server(McpServerConfig(
        name="filesystem",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    ))
    # tools == ["mcp.filesystem.read_file", "mcp.filesystem.write_file", ...]
    await manager.shutdown()
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from synapse.protocols.mcp import McpClient, McpServerConfig
from synapse.protocols.tool import ToolRegistry
from synapse.core.events import EventBus

from synapse.modules.mcp.official_sdk import OfficialSdkMcpClient
from synapse.modules.mcp.wrappers import McpToolWrapper

logger = logging.getLogger(__name__)


class McpManager:
    """Manages the lifecycle of multiple MCP server connections.

    Each server is identified by its ``config.name``.  Adding a server
    with the same name as an existing one will disconnect the old server
    first.  Tools are registered in the ``ToolRegistry`` with fully-
    qualified names in the form ``mcp.<server_name>.<tool_name>``.
    """

    def __init__(
        self,
        tool_registry: ToolRegistry,
        event_bus: EventBus | None = None,
    ) -> None:
        self._tool_registry = tool_registry
        self._event_bus = event_bus

        # server_name -> OfficialSdkMcpClient
        self._clients: dict[str, OfficialSdkMcpClient] = {}

        # server_name -> list of fully-qualified tool names
        self._tool_names: dict[str, list[str]] = {}
        self._connected_loop = None

    # -- Public API ----------------------------------------------------------

    @property
    def connected(self) -> bool:
        """True once at least one server has been connected (and not yet
        shut down). Used by the host to avoid reconnecting on every run."""
        return bool(self._clients)

    async def add_server(self, config: McpServerConfig) -> list[str]:
        """Connect to the MCP server described by *config* and register its
        tools in the tool registry.

        If a server with the same name is already connected it is removed
        first (tools unregistered and client disconnected).

        Returns
        -------
        list[str]
            Fully-qualified tool names registered for this server, e.g.
            ``["mcp.filesystem.read_file", "mcp.filesystem.write_file"]``.
        """
        # If a server with the same name already exists, remove it first
        if config.name in self._clients:
            await self.remove_server(config.name)

        # 1. Create and connect client
        client = OfficialSdkMcpClient()
        await client.connect(config)
        self._clients[config.name] = client

        # 2. Discover tools
        tool_schemas = await client.list_tools()

        # 3. Create wrapper and register each tool
        tool_names: list[str] = []
        for tool_schema in tool_schemas:
            wrapper = McpToolWrapper(
                client=client,
                server_name=config.name,
                tool_schema=tool_schema,
                risk_level=config.risk_level,
            )
            self._tool_registry.register(wrapper)
            tool_names.append(wrapper.name)

        self._tool_names[config.name] = tool_names

        logger.info(
            "MCP server '%s' connected: %d tools registered.",
            config.name,
            len(tool_names),
        )

        return tool_names

    async def connect_all(self, servers: list[McpServerConfig]) -> None:
        """Connect to every server in *servers* and register their tools.

        Intended to be called from the same event loop that will later invoke
        the tools (e.g. at the start of a run), so the MCP receiver tasks stay
        alive for the duration of the session.
        """
        for config in servers:
            await self.add_server(config)
        self._connected_loop = asyncio.get_running_loop()

    async def ensure_current_loop(self, servers: list[McpServerConfig]) -> None:
        """Connect — or reconnect on the current event loop — every server.

        MCP receiver tasks are bound to the loop that ran ``connect()``. A host
        that ``asyncio.run()``s each task (the CLI does) gets a fresh loop per
        run, so a client from an earlier run is stale: its streams reference a
        closed loop and tool calls hang. Clients whose loop differs are torn
        down and re-established on the running loop.
        """
        current = asyncio.get_running_loop()
        if self._connected_loop is current and all(
            config.name in self._clients for config in servers
        ):
            return
        for name in list(self._clients):
            if self._clients[name].loop is not current:
                await self.remove_server(name)
        for config in servers:
            if config.name not in self._clients:
                await self.add_server(config)
        self._connected_loop = current

    async def remove_server(self, name: str) -> None:
        """Unregister all tools belonging to *name* and disconnect its client.

        Safe to call for a server that is not connected (no-op).
        """
        if name not in self._clients:
            return

        # 1. Unregister tools from registry
        for tool_name in self._tool_names.get(name, []):
            self._tool_registry.unregister(tool_name)

        # 2. Disconnect client
        client = self._clients.pop(name)
        await client.disconnect()

        # 3. Clean up bookkeeping
        self._tool_names.pop(name, None)

        logger.info("MCP server '%s' removed.", name)

    async def list_servers(self) -> list[str]:
        """Return the names of all currently-connected MCP servers."""
        return list(self._clients.keys())

    async def shutdown(self) -> None:
        """Disconnect all servers and unregister all MCP tools.

        Should be called at process exit or when the Synapse instance
        is no longer needed.
        """
        server_names = list(self._clients.keys())
        for name in server_names:
            await self.remove_server(name)

        logger.info("McpManager shutdown complete (%d servers).", len(server_names))
