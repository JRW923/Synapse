"""Tests for OfficialSdkMcpClient — mock-based, no real MCP servers required."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from synapse.modules.mcp.official_sdk import OfficialSdkMcpClient
from synapse.protocols.mcp import McpServerConfig


# ---------------------------------------------------------------------------
# Helpers: build fake MCP SDK objects that mimic pydantic model_dump() output
# ---------------------------------------------------------------------------
def _fake_tool(name="test_tool", description="A test tool", input_schema=None):
    """Create a mock Tool object with model_dump()."""
    if input_schema is None:
        input_schema = {"type": "object", "properties": {}}

    tool = MagicMock()
    tool.model_dump.return_value = {
        "name": name,
        "description": description,
        "inputSchema": input_schema,
        "title": None,
        "outputSchema": None,
        "icons": None,
        "annotations": None,
        "meta": None,
        "execution": None,
    }
    return tool


def _fake_call_tool_result(content=None, is_error=False):
    """Create a mock CallToolResult with model_dump()."""
    if content is None:
        text_block = MagicMock()
        text_block.model_dump.return_value = {"type": "text", "text": "hello"}
        content = [text_block]

    result = MagicMock()
    result.model_dump.return_value = {
        "meta": None,
        "content": [c.model_dump() for c in content],
        "structuredContent": None,
        "isError": is_error,
    }
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_stdio():
    """connect() with stdio transport should set connected=True."""
    client = OfficialSdkMcpClient()

    # stdio_client is imported lazily from mcp.client.stdio
    with patch("mcp.client.stdio.stdio_client") as mock_stdio:
        mock_read = MagicMock()
        mock_write = MagicMock()
        mock_stdio.return_value.__aenter__ = AsyncMock(
            return_value=(mock_read, mock_write),
        )
        mock_stdio.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch("synapse.modules.mcp.official_sdk.ClientSession") as MockSession:
            mock_session = MockSession.return_value
            mock_session.initialize = AsyncMock()

            config = McpServerConfig(
                name="test-server",
                transport="stdio",
                command="python",
                args=["-m", "mcp_server"],
            )
            await client.connect(config)

    assert client.connected is True


@pytest.mark.asyncio
async def test_list_tools():
    """list_tools() should return tool schemas as a list of dicts."""
    client = OfficialSdkMcpClient()

    with patch("mcp.client.stdio.stdio_client") as mock_stdio:
        mock_read = MagicMock()
        mock_write = MagicMock()
        mock_stdio.return_value.__aenter__ = AsyncMock(
            return_value=(mock_read, mock_write),
        )
        mock_stdio.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch("synapse.modules.mcp.official_sdk.ClientSession") as MockSession:
            mock_session = MockSession.return_value
            mock_session.initialize = AsyncMock()

            fake_tools = [
                _fake_tool(name="echo", description="Echo back input"),
                _fake_tool(name="add", description="Add two numbers"),
            ]
            mock_list_result = MagicMock()
            mock_list_result.tools = fake_tools
            mock_session.list_tools = AsyncMock(return_value=mock_list_result)

            config = McpServerConfig(
                name="test-server",
                transport="stdio",
                command="python",
            )
            await client.connect(config)

            tools = await client.list_tools()

    assert len(tools) == 2
    assert tools[0]["name"] == "echo"
    assert tools[1]["name"] == "add"
    assert tools[0]["description"] == "Echo back input"
    assert "inputSchema" in tools[0]


@pytest.mark.asyncio
async def test_call_tool():
    """call_tool() should delegate to the SDK session and return a dict result."""
    client = OfficialSdkMcpClient()

    with patch("mcp.client.stdio.stdio_client") as mock_stdio:
        mock_read = MagicMock()
        mock_write = MagicMock()
        mock_stdio.return_value.__aenter__ = AsyncMock(
            return_value=(mock_read, mock_write),
        )
        mock_stdio.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch("synapse.modules.mcp.official_sdk.ClientSession") as MockSession:
            mock_session = MockSession.return_value
            mock_session.initialize = AsyncMock()

            fake_result = _fake_call_tool_result(
                content=[MagicMock(model_dump=lambda: {"type": "text", "text": "result text"})],
                is_error=False,
            )
            mock_session.call_tool = AsyncMock(return_value=fake_result)

            config = McpServerConfig(
                name="test-server",
                transport="stdio",
                command="python",
            )
            await client.connect(config)

            result = await client.call_tool("echo", {"message": "hello"})

    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    assert result["content"][0]["text"] == "result text"
