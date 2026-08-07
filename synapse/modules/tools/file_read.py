"""Read file tool."""

from pathlib import Path
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory

# Cap on emitted content so a huge file can't blow the context window.
MAX_OUTPUT_CHARS = 60_000


class ReadTool:
    name = "read"
    description = "Read a file's contents with line numbers. Supports offset/limit to read a slice. Use to inspect code, configs, or logs."
    parameters = ToolSchema(
        name="read",
        description=(
            "Read a file's contents. Output is prefixed with line numbers. "
            "Use 'offset' (1-based) and 'limit' to read a slice of a large file."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"},
                "offset": {"type": "integer", "description": "1-based start line (default 1)"},
                "limit": {"type": "integer", "description": "Max number of lines to return (default: all)"},
            },
            "required": ["path"],
        },
    )
    requires_sandbox = False
    risk_level = RiskLevel.READ_ONLY
    category = ToolCategory.FILE

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        path = Path(params["path"])
        offset = max(1, int(params.get("offset", 1) or 1))
        limit = params.get("limit")
        limit = int(limit) if limit else None
        meta = ToolCallMetadata(tool_name="read")
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
            lines = raw.splitlines()
            slice_lines = lines[offset - 1: (offset - 1 + limit) if limit else None]

            numbered = []
            for i, line in enumerate(slice_lines, start=offset):
                numbered.append(f"{i:6}\t{line}")
            content = "\n".join(numbered)

            truncated = False
            if len(content) > MAX_OUTPUT_CHARS:
                content = content[:MAX_OUTPUT_CHARS]
                truncated = True

            note = (
                f"[file: {path}, lines {offset}-"
                f"{offset + len(slice_lines) - 1} of {len(lines)}]"
                + (" [output truncated]" if truncated else "")
            )
            return ToolResult(success=True, output=f"{note}\n{content}", metadata=meta)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e), metadata=meta)
