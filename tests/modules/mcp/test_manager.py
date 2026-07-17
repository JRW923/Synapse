"""Tests for McpManager."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from synapse.protocols.mcp import McpServerConfig
from synapse.modules.tools.registry import DefaultToolRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_tools():
    """Return a list of fake MCP tool schema dicts (the format returned by
    OfficialSdkMcpClient.list_tools())."""
    return [
        {
            "name": "read_file",
            "description": "Read a file from the server",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "write_file",
            "description": "Write content to a file on the server",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "content": {"type": "string", "description": "File content"},
                },
                "required": ["path", "content"],
            },
        },
    ]


def _make_mock_client(fake_tools=None):
    """Create a mock OfficialSdkMcpClient instance.

    The mock has async connect / disconnect / list_tools methods (list_tools
    returns *fake_tools* if provided, otherwise an empty list).
    """
    if fake_tools is None:
        fake_tools = []

    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.list_tools = AsyncMock(return_value=fake_tools)
    client.connected = True
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_server_registers_tools():
    """add_server() should connect, list tools, wrap each one, and register
    them in the tool_registry."""
    fake_tools = _make_fake_tools()
    mock_client = _make_mock_client(fake_tools)

    registry = DefaultToolRegistry()

    with patch(
        "synapse.modules.mcp.manager.OfficialSdkMcpClient",
        return_value=mock_client,
    ):
        from synapse.modules.mcp.manager import McpManager

        manager = McpManager(tool_registry=registry)

        config = McpServerConfig(
            name="filesystem",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem"],
        )
        tool_names = await manager.add_server(config)

    # Verify client.connect() was called with the config
    mock_client.connect.assert_called_once_with(config)

    # Verify client.list_tools() was called
    mock_client.list_tools.assert_called_once()

    # Verify two tools were registered
    assert len(tool_names) == 2
    assert "mcp.filesystem.read_file" in tool_names
    assert "mcp.filesystem.write_file" in tool_names

    # Verify tools are retrievable from the registry
    all_tools = registry.list_all()
    assert len(all_tools) >= 2  # the registry may have other tools

    read_tool = registry.get("mcp.filesystem.read_file")
    assert read_tool.name == "mcp.filesystem.read_file"
    assert read_tool.description == "Read a file from the server"

    write_tool = registry.get("mcp.filesystem.write_file")
    assert write_tool.name == "mcp.filesystem.write_file"


@pytest.mark.asyncio
async def test_remove_server_unregisters():
    """remove_server() should unregister all tools for that server and
    disconnect the client."""
    fake_tools = _make_fake_tools()
    mock_client = _make_mock_client(fake_tools)

    registry = DefaultToolRegistry()

    with patch(
        "synapse.modules.mcp.manager.OfficialSdkMcpClient",
        return_value=mock_client,
    ):
        from synapse.modules.mcp.manager import McpManager

        manager = McpManager(tool_registry=registry)

        config = McpServerConfig(
            name="filesystem",
            transport="stdio",
            command="npx",
        )
        await manager.add_server(config)

    # Verify tools are registered
    assert registry.get("mcp.filesystem.read_file") is not None
    assert registry.get("mcp.filesystem.write_file") is not None

    # Now remove the server
    await manager.remove_server("filesystem")

    # Verify tools are unregistered
    with pytest.raises(KeyError):
        registry.get("mcp.filesystem.read_file")
    with pytest.raises(KeyError):
        registry.get("mcp.filesystem.write_file")

    # Verify client.disconnect() was called
    mock_client.disconnect.assert_called_once()

    # Verify server is no longer listed
    servers = await manager.list_servers()
    assert "filesystem" not in servers


@pytest.mark.asyncio
async def test_tools_have_mcp_prefix():
    """All tools registered by McpManager must have the 'mcp.<server_name>.' prefix."""
    fake_tools = _make_fake_tools()
    mock_client = _make_mock_client(fake_tools)

    registry = DefaultToolRegistry()

    with patch(
        "synapse.modules.mcp.manager.OfficialSdkMcpClient",
        return_value=mock_client,
    ):
        from synapse.modules.mcp.manager import McpManager

        manager = McpManager(tool_registry=registry)

        config = McpServerConfig(
            name="my-server",
            transport="stdio",
            command="python",
        )
        tool_names = await manager.add_server(config)

    # Every returned tool name must start with 'mcp.my-server.'
    assert len(tool_names) > 0
    for name in tool_names:
        assert name.startswith("mcp.my-server."), (
            f"Tool name '{name}' does not have the expected prefix"
        )

    # Also verify via registry
    for tool in registry.list_all():
        if tool.category.value == "integration":
            assert tool.name.startswith("mcp."), (
                f"Registered MCP tool '{tool.name}' does not start with 'mcp.'"
            )


@pytest.mark.asyncio
async def test_list_servers():
    """list_servers() should return the names of all connected servers."""
    mock_client = _make_mock_client()
    registry = DefaultToolRegistry()

    with patch(
        "synapse.modules.mcp.manager.OfficialSdkMcpClient",
        return_value=mock_client,
    ):
        from synapse.modules.mcp.manager import McpManager

        manager = McpManager(tool_registry=registry)

        # Initially empty
        assert await manager.list_servers() == []

        # Add two servers
        await manager.add_server(McpServerConfig(name="server-a", transport="stdio", command="echo"))
        assert await manager.list_servers() == ["server-a"]

        # Use a separate mock for the second server
        mock_client2 = _make_mock_client()
        with patch(
            "synapse.modules.mcp.manager.OfficialSdkMcpClient",
            return_value=mock_client2,
        ):
            await manager.add_server(McpServerConfig(name="server-b", transport="stdio", command="ls"))
        assert sorted(await manager.list_servers()) == ["server-a", "server-b"]


@pytest.mark.asyncio
async def test_shutdown_disconnects_all():
    """shutdown() should disconnect and unregister all servers."""
    mock_client_a = _make_mock_client(_make_fake_tools())
    registry = DefaultToolRegistry()

    with patch(
        "synapse.modules.mcp.manager.OfficialSdkMcpClient",
        return_value=mock_client_a,
    ):
        from synapse.modules.mcp.manager import McpManager

        manager = McpManager(tool_registry=registry)
        await manager.add_server(McpServerConfig(name="srv-a", transport="stdio", command="cmd-a"))

    mock_client_b = _make_mock_client(_make_fake_tools())
    with patch(
        "synapse.modules.mcp.manager.OfficialSdkMcpClient",
        return_value=mock_client_b,
    ):
        await manager.add_server(McpServerConfig(name="srv-b", transport="stdio", command="cmd-b"))

    assert len(await manager.list_servers()) == 2

    await manager.shutdown()

    assert await manager.list_servers() == []
    mock_client_a.disconnect.assert_called_once()
    mock_client_b.disconnect.assert_called_once()

    # Tools should be unregistered
    with pytest.raises(KeyError):
        registry.get("mcp.srv-a.read_file")
    with pytest.raises(KeyError):
        registry.get("mcp.srv-b.read_file")
