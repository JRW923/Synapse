"""MCP Tool Wrapper — wraps a MCP tool as a Synapse Tool Protocol."""

from synapse.protocols.tool import (
    ToolResult,
    ToolCallMetadata,
    ToolSchema,
    RiskLevel,
    ToolCategory,
)
from synapse.protocols.mcp import McpClient


class McpToolWrapper:
    """Wraps a MCP tool schema as a Synapse Tool Protocol.

    Holds references to both the McpClient (for delegation) and the
    server_name (for constructing the fully-qualified tool name).

    Attributes:
        name: Fully-qualified name in the form ``mcp.<server>.<tool>``.
        description: Human-readable description from the MCP tool schema.
        parameters: Synapse ToolSchema converted from the MCP ``inputSchema``.
        risk_level: Configurable risk level (default READ_ONLY).
        category: Always ``ToolCategory.INTEGRATION``.
        requires_sandbox: Always ``False`` (MCP tools run externally).
    """

    requires_sandbox = False

    def __init__(
        self,
        client: McpClient,
        server_name: str,
        tool_schema: dict,
        risk_level: RiskLevel = RiskLevel.EXTERNAL,
    ):
        self._client: McpClient = client
        self._server_name: str = server_name
        self._tool_name: str = tool_schema["name"]

        qualified_name = f"mcp.{server_name}.{self._tool_name}"

        self.name: str = qualified_name
        self.description: str = tool_schema.get("description", "")
        self.parameters: ToolSchema = ToolSchema(
            name=qualified_name,
            description=self.description,
            parameters=tool_schema.get("inputSchema", {}),
        )
        self.risk_level: RiskLevel = risk_level
        self.category: ToolCategory = ToolCategory.INTEGRATION

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        """Delegate execution to the underlying McpClient.call_tool().

        Args:
            params: Tool arguments matching the MCP tool's inputSchema.
            sandbox: Ignored (MCP tools run on the remote server).

        Returns:
            ToolResult with success=True and the MCP response content,
            or success=False with the error message on failure.
        """
        meta = ToolCallMetadata(tool_name=self.name)
        try:
            result = await self._client.call_tool(self._tool_name, params)
            output = self._extract_content(result)
            # The MCP protocol marks tool errors with isError; without this an
            # errored server response would be reported as a success.
            if result.get("isError"):
                return ToolResult(
                    success=False, output="", error=output or "MCP tool returned an error", metadata=meta,
                )
            return ToolResult(success=True, output=output, metadata=meta)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e), metadata=meta)

    @staticmethod
    def _extract_content(result: dict) -> str:
        """Extract text content from an MCP call_tool response.

        Handles both simple string content and list-of-content-blocks
        (the standard MCP protocol format).
        """
        content = result.get("content", "")
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(item.get("text", str(item)))
                else:
                    parts.append(str(item))
            return "\n".join(parts)
        return str(content)
