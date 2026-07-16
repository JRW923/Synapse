"""Git tool — read-only git operations (log, diff, status, show)."""

import asyncio
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory

READ_ONLY_COMMANDS = {"log", "diff", "show", "status", "blame", "branch", "tag", "rev-parse"}


class GitTool:
    name = "git"
    description = "Run read-only git commands: log, diff, show, status, blame, branch, tag."
    parameters = ToolSchema(
        name="git",
        description="Git operations",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Git subcommand and args (e.g. 'log --oneline -5')"},
                "cwd": {"type": "string", "description": "Working directory"},
            },
            "required": ["command"],
        },
    )
    requires_sandbox = True
    risk_level = RiskLevel.READ_ONLY
    category = ToolCategory.CODE_UNDERSTANDING

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        command = params["command"]
        cwd = params.get("cwd", ".")
        meta = ToolCallMetadata(tool_name="git")

        subcommand = command.split()[0] if command.strip() else ""
        if subcommand not in READ_ONLY_COMMANDS:
            return ToolResult(success=False, output="", error=f"Git '{subcommand}' is not allowed (read-only commands only)", metadata=meta)

        full_cmd = f"git {command}"
        try:
            proc = await asyncio.create_subprocess_shell(
                full_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                proc.kill()
                return ToolResult(success=False, output="", error="Git command timed out", metadata=meta)

            output = stdout.decode(errors="ignore")
            if proc.returncode == 0:
                return ToolResult(success=True, output=output[:50000], metadata=meta)
            return ToolResult(success=False, output=output, error=stderr.decode(errors="ignore"), metadata=meta)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e), metadata=meta)
