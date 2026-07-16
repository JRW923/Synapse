"""Grep search tool — regex content search."""

import subprocess
from pathlib import Path
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory


class GrepTool:
    name = "grep"
    description = "Search file contents with a regex pattern using ripgrep."
    parameters = ToolSchema(
        name="grep",
        description="Search code with regex",
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

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        pattern = params["pattern"]
        search_path = params.get("path", ".")
        meta = ToolCallMetadata(tool_name="grep")
        try:
            # Try ripgrep first, fall back to Python
            result = subprocess.run(
                ["rg", "--no-heading", "-n", "--color=never", pattern, search_path],
                capture_output=True, text=True, timeout=30,
            )
            output = result.stdout.strip() or "(no matches)"
            return ToolResult(success=True, output=output[:50000], metadata=meta)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            # Fallback: simple Python grep
            try:
                lines = []
                root = Path(search_path)
                import re
                regex = re.compile(pattern)
                for f in root.rglob("*.py") if root.is_dir() else [root]:
                    if not f.is_file():
                        continue
                    try:
                        for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                            if regex.search(line):
                                lines.append(f"{f}:{i}:{line}")
                    except Exception:
                        continue
                    if len(lines) > 500:
                        break
                output = "\n".join(lines[:500]) or "(no matches)"
                return ToolResult(success=True, output=output[:50000], metadata=meta)
            except Exception as e:
                return ToolResult(success=False, output="", error=str(e), metadata=meta)
