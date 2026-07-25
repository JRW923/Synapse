"""Tests for s04 用户钩子.

验收：注册一个 PostToolUse 钩子，断言命令被执行且拿到事件 payload。
"""

from __future__ import annotations

import asyncio
import json
import os

from synapse.modules.hooks import HookRunner
from synapse.core.events import EventBus
from synapse.protocols.events import ToolCallCompleted, ToolCallStarted
from synapse.config.schema import SynapseConfig


async def test_post_tool_use_hook_runs_and_gets_payload(tmp_path):
    marker = tmp_path / "hook_out.txt"
    os.environ["TEST_HOOK_OUT"] = str(marker)
    try:
        # 钩子命令读取 SYNAPSE_PAYLOAD 并写入 marker 文件
        cmd = (
            "python -c \"import os; "
            "open(os.environ['TEST_HOOK_OUT'],'w').write(os.environ.get('SYNAPSE_PAYLOAD',''))\""
        )
        runner = HookRunner({"tool_call_completed": [cmd]})
        bus = EventBus()
        runner.attach(bus)

        ev = ToolCallCompleted(
            session_id="s1", agent_id="a1", tool_name="write",
            success=True, duration_ms=5, files_touched=["x.py"],
        )
        await bus.emit(ev)

        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(0.02)

        assert marker.exists(), "钩子命令应被执行"
        data = json.loads(marker.read_text(encoding="utf-8"))
        assert data["event_type"] == "tool_call_completed"
        assert data["tool_name"] == "write"
    finally:
        os.environ.pop("TEST_HOOK_OUT", None)


async def test_unrelated_event_does_not_trigger(tmp_path):
    other = tmp_path / "other.txt"
    os.environ["TEST_HOOK_OUT2"] = str(other)
    try:
        cmd = "python -c \"import os; open(os.environ['TEST_HOOK_OUT2'],'w').write('x')\""
        runner = HookRunner({"tool_call_completed": [cmd]})
        bus = EventBus()
        runner.attach(bus)
        # 触发一个不在 hooks 里的事件类型
        await bus.emit(ToolCallStarted(session_id="s", agent_id="a", tool_name="read",
                                       tool_params={}))
        await asyncio.sleep(0.1)
        assert not other.exists()
    finally:
        os.environ.pop("TEST_HOOK_OUT2", None)


def test_config_hooks_section():
    cfg = SynapseConfig(hooks={"hooks": {"tool_call_completed": ["echo hi"]}})
    assert cfg.hooks.hooks["tool_call_completed"] == ["echo hi"]
