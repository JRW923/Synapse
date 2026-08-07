"""Shell command execution tool — runs in sandbox."""

import asyncio
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory
from synapse.modules.tools.background import BackgroundTaskManager

# Cap on emitted output so a verbose command can't blow the context window.
# Keeps shell consistent with grep/git tools (50 KB).
MAX_SHELL_OUTPUT = 50_000


def _cap(text: str) -> str:
    if text and len(text) > MAX_SHELL_OUTPUT:
        return text[:MAX_SHELL_OUTPUT] + f"\n\n[shell output truncated to {MAX_SHELL_OUTPUT} chars]"
    return text


class ShellTool:
    name = "shell"
    description = (
        "Run a shell command (curl, wget, mkdir, find, ls, cat, echo, python, git, etc.). "
        "Use curl to fetch web pages and APIs for real-time information. "
        "Set run_in_background=true to start the command in the background and get a task_id "
        "handle immediately; read its result later by passing read_task_id."
    )
    parameters = ToolSchema(
        name="shell",
        description="Run a shell command. Available commands include: curl (HTTP requests), wget (download), mkdir, find, ls, cat, echo, python, git, and more.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run"},
                "cwd": {"type": "string", "description": "Working directory (optional)"},
                "run_in_background": {"type": "boolean", "description": "Run in background; return a task_id handle immediately (s13)"},
                "read_task_id": {"type": "string", "description": "Read the result of a previously backgrounded task (s13)"},
            },
            "required": ["command"],
        },
    )
    requires_sandbox = True
    risk_level = RiskLevel.EXECUTE
    category = ToolCategory.EXECUTION

    def __init__(self, background_manager: BackgroundTaskManager | None = None):
        self.background_manager = background_manager

    async def execute(self, params: dict, sandbox=None, timeout: int | None = None) -> ToolResult:
        command = params["command"]
        cwd = params.get("cwd", ".")
        timeout = timeout or 120
        meta = ToolCallMetadata(tool_name="shell")

        # s13 — read a previously backgrounded task's result.
        read_task_id = params.get("read_task_id")
        if read_task_id:
            if self.background_manager is None:
                return ToolResult(success=False, output="", error="Background execution is not enabled", metadata=meta)
            res = self.background_manager.get_result(read_task_id)
            if res is None:
                return ToolResult(success=False, output="", error=f"Unknown background task: {read_task_id}", metadata=meta)
            if res == "still running":
                return ToolResult(success=True, output=f"[background task {read_task_id} still running]", metadata=meta)
            return res

        # s13 — start in background, return a handle immediately.
        if params.get("run_in_background") and self.background_manager is not None:
            task_id = await self.background_manager.run(command, cwd, sandbox, timeout)
            meta.sandbox_used = sandbox is not None
            return ToolResult(
                success=True,
                output=f"Background task started: {task_id}",
                metadata=meta,
            )

        try:
            if sandbox is not None:
                result = await sandbox.execute(command, cwd=cwd, timeout=timeout)
                meta.sandbox_used = True
                meta.duration_ms = 0
                if result.exit_code == 0:
                    return ToolResult(success=True, output=_cap(result.stdout), metadata=meta)
                else:
                    return ToolResult(success=False, output=_cap(result.stdout), error=_cap(result.stderr), metadata=meta)

            # Fallback: no sandbox (warning mode)
            proc = await asyncio.create_subprocess_shell(
                command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ToolResult(success=False, output="", error=f"Command timed out after {timeout}s", metadata=meta)

            if proc.returncode == 0:
                return ToolResult(success=True, output=_cap(stdout.decode(errors="ignore")), metadata=meta)
            return ToolResult(success=False, output=_cap(stdout.decode(errors="ignore")), error=_cap(stderr.decode(errors="ignore")), metadata=meta)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e), metadata=meta)
