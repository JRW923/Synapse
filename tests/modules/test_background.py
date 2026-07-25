"""Tests for s13 后台执行.

验收：后台 shell 立即返回 handle，稍后 BackgroundResult 事件携带 stdout；
可用 read_task_id 在后续轮次读取结果。
"""

from __future__ import annotations

import asyncio

from synapse.modules.tools.background import BackgroundTaskManager
from synapse.modules.tools.shell import ShellTool
from synapse.core.events import EventBus
from synapse.protocols.tool import ToolResult


class _FakeSandbox:
    def __init__(self, stdout="ok"):
        self.stdout = stdout
    async def execute(self, command, cwd, timeout):
        await asyncio.sleep(0.02)
        return type("R", (), {"exit_code": 0, "stdout": f"{self.stdout}:{command}", "stderr": ""})()


async def test_background_returns_handle_then_event():
    bus = EventBus()
    captured = []
    async def cap(e):
        captured.append(e)
    bus.subscribe("background_result", cap)

    mgr = BackgroundTaskManager()
    mgr.set_run_context(bus, "sess")
    tool = ShellTool(background_manager=mgr)
    sandbox = _FakeSandbox("out")

    res = await tool.execute({"command": "echo hi", "run_in_background": True}, sandbox=sandbox)
    assert res.success
    assert res.output.startswith("Background task started: bg-")
    task_id = res.output.split(": ", 1)[1]

    # 后台任务在事件循环里继续跑——等待完成
    for _ in range(100):
        if mgr.is_done(task_id):
            break
        await asyncio.sleep(0.01)

    assert mgr.is_done(task_id)
    bg = [e for e in captured if e.event_type == "background_result"]
    assert bg, "应发出 BackgroundResult 事件"
    assert bg[0].task_id == task_id
    assert bg[0].stdout == "out:echo hi"

    # 后续轮次读取
    read = await tool.execute({"command": "", "read_task_id": task_id}, sandbox=sandbox)
    assert "out:echo hi" in read.output


async def test_foreground_unchanged_without_flag():
    mgr = BackgroundTaskManager()
    tool = ShellTool(background_manager=mgr)
    sandbox = _FakeSandbox("fg")
    res = await tool.execute({"command": "ls"}, sandbox=sandbox)
    assert res.success
    assert res.output == "fg:ls"


async def test_read_unknown_task_errors():
    tool = ShellTool(background_manager=BackgroundTaskManager())
    res = await tool.execute({"command": "", "read_task_id": "bg-999"}, sandbox=_FakeSandbox())
    assert not res.success
    assert "Unknown" in res.error


async def test_manager_no_sandbox_uses_subprocess():
    """无 sandbox 时退化为 asyncio subprocess 后台执行。"""
    mgr = BackgroundTaskManager()
    task_id = await mgr.run("echo hello-bg", ".", None, timeout=10)
    for _ in range(100):
        if mgr.is_done(task_id):
            break
        await asyncio.sleep(0.01)
    assert mgr.is_done(task_id)
    res = mgr.get_result(task_id)
    assert isinstance(res, ToolResult) and res.success
    assert "hello-bg" in res.output
