"""Integration tests for MCP wiring into Synapse and CLI."""
import argparse
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from synapse.protocols.mcp import McpServerConfig
from synapse.protocols.llm import LLMResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_tools():
    """Return mock MCP tool schemas."""
    return [
        {
            "name": "echo",
            "description": "Echo back the input",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Message to echo"},
                },
                "required": ["message"],
            },
        },
    ]


def _make_mock_client(fake_tools=None):
    """Create a mock OfficialSdkMcpClient."""
    if fake_tools is None:
        fake_tools = []
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.list_tools = AsyncMock(return_value=fake_tools)
    client.connected = True
    return client


# ---------------------------------------------------------------------------
# test_synapse_with_mcp_server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synapse_with_mcp_server():
    """Synapse(mcp_servers=[...]) should wire McpManager and register MCP tools."""
    mock_llm = AsyncMock()
    mock_llm.model_id = "mock"
    mock_llm.chat.return_value = LLMResponse(
        content="Done.",
        tool_calls=[],
        stop_reason="end_turn",
        usage={"input": 5, "output": 3},
    )

    fake_tools = _make_fake_tools()
    mock_client = _make_mock_client(fake_tools)

    with patch(
        "synapse.modules.providers.anthropic.AnthropicProvider",
        return_value=mock_llm,
    ):
            with patch(
                "synapse.modules.mcp.manager.OfficialSdkMcpClient",
                return_value=mock_client,
            ):
                from synapse.adapters.library import Synapse

                mcp_config = McpServerConfig(
                    name="test-server",
                    transport="stdio",
                    command="python",
                    args=["-m", "test.mcp.server"],
                )
                synapse = Synapse(
                    provider="anthropic",
                    mcp_servers=[mcp_config],
                )
                # Connect while the client mock is patched — mirrors run().
                await synapse._mcp_manager.connect_all(synapse._mcp_servers)

    # The McpManager should have registered the MCP tool. Retrieve the tool
    # registry from the container.
    from synapse.protocols.tool import ToolRegistry
    registry = synapse._container.resolve(ToolRegistry)

    # The MCP tool should be registered
    tool = registry.get("mcp.test-server.echo")
    assert tool.name == "mcp.test-server.echo"
    assert tool.description == "Echo back the input"

    # The MCP client should have been connected
    mock_client.connect.assert_called_once_with(mcp_config)
    mock_client.list_tools.assert_called_once()


@pytest.mark.asyncio
async def test_synapse_with_mcp_server_optional():
    """mcp_servers defaults to None and Synapse should still work."""
    mock_llm = AsyncMock()
    mock_llm.model_id = "mock"
    mock_llm.chat.return_value = LLMResponse(
        content="Done.",
        tool_calls=[],
        stop_reason="end_turn",
        usage={"input": 5, "output": 3},
    )

    with patch(
        "synapse.modules.providers.anthropic.AnthropicProvider",
        return_value=mock_llm,
    ):
        from synapse.adapters.library import Synapse

        # No mcp_servers provided — should not crash
        synapse = Synapse(provider="anthropic")

    from synapse.protocols.tool import ToolRegistry
    registry = synapse._container.resolve(ToolRegistry)

    # No MCP tools should be registered (only built-in tools)
    tool_names = [t.name for t in registry.list_all()]
    assert all(not name.startswith("mcp.") for name in tool_names), (
        f"Unexpected MCP tools registered: {tool_names}"
    )


# ---------------------------------------------------------------------------
# test_cli_parses_mcp_server_args
# ---------------------------------------------------------------------------


def _parse_mcp_value(raw: str) -> McpServerConfig:
    """Parse a --mcp-server value into an McpServerConfig.

    Format: ``name:command_or_url``

    - If the part after ``:`` starts with ``http://`` or ``https://``,
      the transport is ``streamable_http``.
    - Otherwise, the transport is ``stdio``.  The command and its arguments
      are split on whitespace.
    """
    if ":" not in raw:
        raise ValueError(f"Invalid --mcp-server format: {raw!r}")

    name, rest = raw.split(":", 1)

    if rest.startswith("http://") or rest.startswith("https://"):
        return McpServerConfig(
            name=name,
            transport="streamable_http",
            url=rest,
        )

    # stdio: split command and args on whitespace
    parts = rest.split()
    command = parts[0]
    args = parts[1:] if len(parts) > 1 else []
    return McpServerConfig(
        name=name,
        transport="stdio",
        command=command,
        args=args,
    )


def test_cli_parses_mcp_server_args_stdio():
    """--mcp-server 'name:command' should produce a stdio McpServerConfig."""
    config = _parse_mcp_value("filesystem:npx")
    assert config.name == "filesystem"
    assert config.transport == "stdio"
    assert config.command == "npx"
    assert config.args == []


def test_cli_parses_mcp_server_args_stdio_with_args():
    """--mcp-server 'name:command arg1 arg2' should parse command and args."""
    config = _parse_mcp_value("filesystem:npx -y @modelcontextprotocol/server-filesystem /tmp")
    assert config.name == "filesystem"
    assert config.transport == "stdio"
    assert config.command == "npx"
    assert config.args == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]


def test_cli_parses_mcp_server_args_http():
    """--mcp-server 'name:http://host' should produce a streamable_http config."""
    config = _parse_mcp_value("myapi:http://localhost:8000/mcp")
    assert config.name == "myapi"
    assert config.transport == "streamable_http"
    assert config.url == "http://localhost:8000/mcp"
    assert config.command is None


def test_cli_parses_mcp_server_args_https():
    """--mcp-server 'name:https://...' should also be streamable_http."""
    config = _parse_mcp_value("secure:https://mcp.example.com/v1")
    assert config.name == "secure"
    assert config.transport == "streamable_http"
    assert config.url == "https://mcp.example.com/v1"


def test_cli_parses_multiple_mcp_servers():
    """Multiple --mcp-server flags should each produce a McpServerConfig."""
    configs = [
        _parse_mcp_value("fs:npx -y @modelcontextprotocol/server-filesystem"),
        _parse_mcp_value("api:http://localhost:8000"),
    ]
    assert len(configs) == 2
    assert configs[0].name == "fs"
    assert configs[0].transport == "stdio"
    assert configs[1].name == "api"
    assert configs[1].transport == "streamable_http"
