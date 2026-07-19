"""Glob file tool — filename pattern matching."""

from pathlib import Path
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory


class GlobTool:
    name = "glob"
    description = "Find files by name pattern (e.g., **/*.py for all Python files, **/*test* for test files)."
    parameters = ToolSchema(
        name="glob",
        description="Find files matching a glob pattern. Use to explore project structure, locate specific file types.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (e.g. **/*.py)"},
                "path": {"type": "string", "description": "Directory to search in"},
            },
            "required": ["pattern"],
        },
    )
    requires_sandbox = False
    risk_level = RiskLevel.READ_ONLY
    category = ToolCategory.FILE

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        pattern = params["pattern"]
        root = Path(params.get("path", "."))
        meta = ToolCallMetadata(tool_name="glob")
        try:
            matches = sorted(root.glob(pattern))
            # Limit output to 100 matches
            lines = [str(m) for m in matches[:100]]
            output = "\n".join(lines) if lines else "(no matches)"
            return ToolResult(success=True, output=output, metadata=meta)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e), metadata=meta)
