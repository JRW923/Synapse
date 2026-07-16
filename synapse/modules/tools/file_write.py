"""Write file tool."""

from pathlib import Path
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory


class WriteTool:
    name = "write"
    description = "Write content to a file, overwriting if it exists."
    parameters = ToolSchema(
        name="write",
        description="Write a file",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
    )
    requires_sandbox = True
    risk_level = RiskLevel.WRITE_LOCAL
    category = ToolCategory.FILE

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        path = Path(params["path"])
        content = params["content"]
        meta = ToolCallMetadata(tool_name="write")
        meta.files_touched = [str(path)]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return ToolResult(success=True, output=f"Wrote {len(content)} bytes to {path}", metadata=meta)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e), metadata=meta)
