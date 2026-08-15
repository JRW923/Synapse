"""后台任务管理器 (s13).

让 shell 命令在后台运行：立即返回 handle，命令结束后经 ``BackgroundResult``
事件回传 stdout/stderr，并缓存结果供后续轮次读取。

ponytail: 进程内 ``asyncio.create_task``，无跨进程持久化；进程退出后台任务即丢。
管理器实例需在 ReActPlanner 与 ShellTool 间共享（同一 run 内）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from synapse.protocols.tool import ToolResult
from synapse.protocols.events import BackgroundResult


@dataclass
class _BgTask:
    task_id: str
    command: str
    done: bool = False
    result: Any = None


class BackgroundTaskManager:
    def __init__(self):
        self._tasks: dict[str, _BgTask] = {}
        self._runners: set[asyncio.Task[None]] = set()
        self._counter = 0
        self._event_bus = None
        self._session_id = ""

    def set_run_context(self, event_bus, session_id: str) -> None:
        self._event_bus = event_bus
        self._session_id = session_id

    def _next_id(self) -> str:
        self._counter += 1
        return f"bg-{self._counter}"

    async def run(self, command: str, cwd: str, sandbox, timeout: int = 120) -> str:
        """Start *command* in the background; return a task_id handle now."""
        task_id = self._next_id()
        self._tasks[task_id] = _BgTask(task_id=task_id, command=command)
        runner = asyncio.create_task(
            self._run(task_id, command, cwd, sandbox, timeout)
        )
        self._runners.add(runner)
        runner.add_done_callback(self._runners.discard)
        return task_id

    async def _run(self, task_id: str, command: str, cwd: str, sandbox, timeout: int) -> None:
        bt = self._tasks[task_id]
        proc = None
        try:
            try:
                if sandbox is not None:
                    r = await sandbox.execute(command, cwd=cwd, timeout=timeout)
                    success = r.exit_code == 0
                    stdout, stderr = r.stdout, r.stderr
                else:
                    proc = await asyncio.create_subprocess_shell(
                        command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=cwd,
                    )
                    stdout_b, stderr_b = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout,
                    )
                    success = proc.returncode == 0
                    stdout = stdout_b.decode(errors="ignore")
                    stderr = stderr_b.decode(errors="ignore")
            except Exception as exc:  # best-effort: never crash the background loop
                success, stdout, stderr = False, "", str(exc)

            bt.result = ToolResult(success=success, output=stdout, error=stderr)
            if self._event_bus is not None:
                await self._event_bus.emit(BackgroundResult(
                    session_id=self._session_id, agent_id="", task_id=task_id,
                    success=success, stdout=stdout, stderr=stderr,
                ))
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            bt.result = ToolResult(
                success=False, output="", error="background task cancelled",
            )
            raise
        finally:
            bt.done = True

    def get_result(self, task_id: str) -> Any:
        """Return the ToolResult once done, 'still running' before, None if unknown."""
        bt = self._tasks.get(task_id)
        if bt is None:
            return None
        return bt.result if bt.done else "still running"

    def is_done(self, task_id: str) -> bool:
        bt = self._tasks.get(task_id)
        return bt is not None and bt.done

    @property
    def pending_count(self) -> int:
        return len(self._runners)

    async def aclose(self) -> None:
        """Cancel and await every command still owned by this manager."""
        runners = tuple(self._runners)
        for runner in runners:
            runner.cancel()
        if runners:
            await asyncio.gather(*runners, return_exceptions=True)
        self._runners.clear()
        self._event_bus = None
        self._session_id = ""


# Process-wide shared manager so ShellTool and ReActPlanner agree on tasks.
_DEFAULT_MANAGER: "BackgroundTaskManager | None" = None


def get_default_manager() -> "BackgroundTaskManager":
    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is None:
        _DEFAULT_MANAGER = BackgroundTaskManager()
    return _DEFAULT_MANAGER
