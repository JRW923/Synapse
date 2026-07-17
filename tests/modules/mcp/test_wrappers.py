"""Tests for McpToolWrapper."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from synapse.modules.mcp.wrappers import McpToolWrapper
from synapse.protocols.tool import RiskLevel, ToolCategory, ToolSchema


@pytest.mark.asyncio
async def test_wrapper_has_mcp_prefix():
    """Verify wrapper name is 'mcp.<server>.<tool>' format."""
    client = MagicMock()
    tool_schema = {
        "name": "read_file",
        "description": "Read a file from the server",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
            },
            "required": ["path"],
        },
    }
    wrapper = McpToolWrapper(client, "filesystem", tool_schema)

    assert wrapper.name == "mcp.filesystem.read_file"
    assert wrapper.description == "Read a file from the server"
    assert wrapper.risk_level == RiskLevel.READ_ONLY
    assert wrapper.category == ToolCategory.INTEGRATION
    assert wrapper.requires_sandbox is False
    assert isinstance(wrapper.parameters, ToolSchema)
    assert wrapper.parameters.name == "mcp.filesystem.read_file"
    assert wrapper.parameters.parameters == tool_schema["inputSchema"]


@pytest.mark.asyncio
async def test_wrapper_delegates_to_client():
    """Mock McpClient, call execute, verify call_tool invoked with correct args."""
    client = MagicMock()
    client.call_tool = AsyncMock(return_value={"content": "file contents here"})

    tool_schema = {
        "name": "read_file",
        "description": "Read a file",
        "inputSchema": {"type": "object", "properties": {}},
    }
    wrapper = McpToolWrapper(client, "filesystem", tool_schema)

    result = await wrapper.execute({"path": "/etc/hosts"})

    client.call_tool.assert_called_once_with("read_file", {"path": "/etc/hosts"})
    assert result.success is True
    assert result.output == "file contents here"


@pytest.mark.asyncio
async def test_wrapper_execute_error():
    """Verify execute returns ToolResult(success=False) on error."""
    client = MagicMock()
    client.call_tool = AsyncMock(side_effect=RuntimeError("MCP server disconnected"))

    tool_schema = {
        "name": "bad_tool",
        "description": "A tool that fails",
        "inputSchema": {"type": "object", "properties": {}},
    }
    wrapper = McpToolWrapper(client, "test", tool_schema)

    result = await wrapper.execute({})
    assert result.success is False
    assert "MCP server disconnected" in result.error


@pytest.mark.asyncio
async def test_wrapper_list_content_format():
    """Verify execute handles MCP list-format content blocks."""
    client = MagicMock()
    client.call_tool = AsyncMock(return_value={
        "content": [
            {"type": "text", "text": "line one"},
            {"type": "text", "text": "line two"},
        ]
    })

    tool_schema = {
        "name": "list_tool",
        "description": "Returns list content",
        "inputSchema": {"type": "object", "properties": {}},
    }
    wrapper = McpToolWrapper(client, "test", tool_schema)

    result = await wrapper.execute({})
    assert result.success is True
    assert "line one" in result.output
    assert "line two" in result.output


@pytest.mark.asyncio
async def test_wrapper_custom_risk_level():
    """Verify risk_level is configurable."""
    client = MagicMock()
    tool_schema = {
        "name": "delete_file",
        "description": "Delete a file",
        "inputSchema": {"type": "object", "properties": {}},
    }
    wrapper = McpToolWrapper(client, "filesystem", tool_schema, risk_level=RiskLevel.WRITE_LOCAL)
    assert wrapper.risk_level == RiskLevel.WRITE_LOCAL
