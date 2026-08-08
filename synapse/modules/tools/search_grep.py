"""Grep search tool — regex content search."""

import asyncio
from pathlib import Path
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory
from synapse.modules.tools.workspace import WorkspacePathError, resolve_workspace_path


class GrepTool:
    name = "grep"
    description = "Search file contents with regex (ripgrep). Find function definitions, imports, error messages, or any text pattern."
    parameters = ToolSchema(
        name="grep",
        description="Search file contents with regex. Use to find where a function/class/import is used, locate error messages, or discover code patterns.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {"type": "string", "description": "Directory or file to search in"},
            },
            "required": ["pattern"],
        },
    )
    requires_sandbox = False
    risk_level = RiskLevel.READ_ONLY
    category = ToolCategory.CODE_UNDERSTANDING

    def __init__(self, workspace_root: str | None = None):
        self._workspace_root = Path(workspace_root).resolve() if workspace_root else None

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        pattern = params["pattern"]
        search_path = params.get("path", ".")
        try:
            search_root = resolve_workspace_path(search_path, self._workspace_root)
        except (WorkspacePathError, ValueError) as e:
            return ToolResult(
                success=False, output="", error=str(e),
                metadata=ToolCallMetadata(tool_name="grep"),
            )
        search_path = str(search_root)
        meta = ToolCallMetadata(tool_name="grep")

        # Try ripgrep first via async subprocess (non-blocking)
        try:
            proc = await asyncio.create_subprocess_exec(
                "rg", "--no-heading", "-n", "--color=never", pattern, search_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=30,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ToolResult(
                    success=False, output="",
                    error="ripgrep timed out after 30s", metadata=meta,
                )

            stdout_str = stdout.decode() if stdout else ""
            stderr_str = stderr.decode() if stderr else ""

            if proc.returncode != 0 and proc.returncode is not None:
                if stderr_str.strip():
                    return ToolResult(
                        success=False, output="",
                        error=stderr_str.strip(), metadata=meta,
                    )
            output = stdout_str.strip() or "(no matches)"
            return ToolResult(success=True, output=output[:50000], metadata=meta)
        except FileNotFoundError:
            # ripgrep not installed — fall through to Python fallback
            pass

        # Fallback: Python grep (runs in thread to avoid blocking the event loop)
        try:
            output = await asyncio.to_thread(self._python_grep, pattern, search_path)
            return ToolResult(success=True, output=output[:50000], metadata=meta)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e), metadata=meta)

    @staticmethod
    def _python_grep(pattern: str, search_path: str) -> str:
        """Synchronous Python regex grep (executed in a thread)."""
        import re
        lines = []
        files_scanned = 0
        max_files = 1000  # 最多扫描 1000 个文件，防止大目录卡死
        root = Path(search_path)
        regex = re.compile(pattern)
        for f in root.rglob("*") if root.is_dir() else [root]:
            if files_scanned >= max_files:
                break
            if not f.is_file():
                continue
            files_scanned += 1
            try:
                for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                    if regex.search(line):
                        lines.append(f"{f}:{i}:{line}")
            except Exception:
                continue
            if len(lines) > 500:
                break
        return "\n".join(lines[:500]) or "(no matches)"
