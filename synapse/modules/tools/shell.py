"""Shell command execution tool — runs in sandbox."""

import asyncio
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory


class ShellTool:
    name = "shell"
    description = "Execute a shell command in a subprocess."
    parameters = ToolSchema(
        name="shell",
        description="Execute a shell command",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run"},
                "cwd": {"type": "string", "description": "Working directory (optional)"},
            },
            "required": ["command"],
        },
    )
    requires_sandbox = True
    risk_level = RiskLevel.EXECUTE
    category = ToolCategory.EXECUTION

    async def execute(self, params: dict, sandbox=None, timeout: int | None = None) -> ToolResult:
        command = params["command"]
        cwd = params.get("cwd", ".")
        timeout = timeout or 120
        meta = ToolCallMetadata(tool_name="shell")

        try:
            if sandbox is not None:
                result = await sandbox.execute(command, cwd=cwd, timeout=timeout)
                meta.sandbox_used = True
                meta.duration_ms = 0
                if result.exit_code == 0:
                    return ToolResult(success=True, output=result.stdout, metadata=meta)
                else:
                    return ToolResult(success=False, output=result.stdout, error=result.stderr, metadata=meta)

            # Fallback: no sandbox (warning mode)
            proc = await asyncio.create_subprocess_shell(
                command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return ToolResult(success=False, output="", error=f"Command timed out after {timeout}s", metadata=meta)

            if proc.returncode == 0:
                return ToolResult(success=True, output=stdout.decode(errors="ignore"), metadata=meta)
            return ToolResult(success=False, output=stdout.decode(errors="ignore"), error=stderr.decode(errors="ignore"), metadata=meta)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e), metadata=meta)
