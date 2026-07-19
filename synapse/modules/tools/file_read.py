"""Read file tool."""

from pathlib import Path
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory


class ReadTool:
    name = "read"
    description = "Read the contents of a file. Use to inspect code, configs, logs, or any text file."
    parameters = ToolSchema(
        name="read",
        description="Read a file's contents. Shows line numbers. Use to examine code, check results, or verify file state.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"}
            },
            "required": ["path"],
        },
    )
    requires_sandbox = False
    risk_level = RiskLevel.READ_ONLY
    category = ToolCategory.FILE

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        path = Path(params["path"])
        meta = ToolCallMetadata(tool_name="read")
        try:
            content = path.read_text(encoding="utf-8")
            meta.duration_ms = 0
            return ToolResult(success=True, output=content, metadata=meta)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e), metadata=meta)
