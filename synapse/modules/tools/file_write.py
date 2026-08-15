"""Write file tool."""

from pathlib import Path
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory
from synapse.modules.tools.workspace import WorkspacePathError, resolve_workspace_path


class WriteTool:
    name = "write"
    description = "Create or overwrite a file with new content. Use to generate code, save results, or create config files."
    parameters = ToolSchema(
        name="write",
        description=(
            "Create or overwrite a file. Provide an absolute path, or a path "
            "relative to the workspace root. Creates parent directories if needed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path, or a path relative to the workspace root",
                },
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
    )
    requires_sandbox = True
    risk_level = RiskLevel.WRITE_LOCAL
    category = ToolCategory.FILE

    def __init__(self, workspace_root: str | None = None):
        # Resolve relative write paths against the workspace root so files land
        # in the project dir, not the (often unrelated) process cwd.
        self._workspace_root = Path(workspace_root).resolve() if workspace_root else None

    def _resolve_path(self, raw: str) -> Path:
        path = Path(raw)
        if not path.is_absolute() and self._workspace_root is not None:
            path = self._workspace_root / path
        return path

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        raw = params["path"]
        try:
            path = resolve_workspace_path(raw, self._workspace_root)
        except (WorkspacePathError, ValueError) as e:
            return ToolResult(
                success=False, output="", error=str(e),
                metadata=ToolCallMetadata(tool_name="write"),
            )
        content = params["content"]
        meta = ToolCallMetadata(tool_name="write")
        meta.files_touched = [str(path)]

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # newline="" preserves exact content (no LF->CRLF translation).
            path.write_text(content, encoding="utf-8", newline="")
            return ToolResult(success=True, output=f"Wrote {len(content)} bytes to {path}", metadata=meta)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e), metadata=meta)
