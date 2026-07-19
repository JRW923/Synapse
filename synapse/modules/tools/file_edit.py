"""Edit file tool — exact string replacement."""

from pathlib import Path
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory


class EditTool:
    name = "edit"
    description = "Replace a specific string in a file. Use for targeted edits: fix bugs, rename symbols, update configs."
    parameters = ToolSchema(
        name="edit",
        description="Replace old_string with new_string in a file. Both must match exactly (including whitespace). Use for targeted changes.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"},
                "old_string": {"type": "string", "description": "Exact text to replace"},
                "new_string": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    )
    requires_sandbox = True
    risk_level = RiskLevel.WRITE_LOCAL
    category = ToolCategory.FILE

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        path = Path(params["path"])
        old = params["old_string"]
        new = params["new_string"]
        meta = ToolCallMetadata(tool_name="edit")
        meta.files_touched = [str(path)]

        try:
            content = path.read_text(encoding="utf-8")
            count = content.count(old)
            if count == 0:
                return ToolResult(success=False, output="", error="old_string not found in file", metadata=meta)
            if count > 1:
                return ToolResult(success=False, output="", error=f"old_string is not unique in file — found {count} occurrences", metadata=meta)
            new_content = content.replace(old, new)
            path.write_text(new_content, encoding="utf-8")
            return ToolResult(success=True, output=f"Replaced 1 occurrence in {path}", metadata=meta)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e), metadata=meta)
