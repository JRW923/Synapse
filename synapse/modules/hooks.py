"""用户生命周期钩子 (s04).

把 config 的 hooks 段（event_type → shell 命令列表）接到 EventBus：当事件发出后，
对应命令经 ``asyncio.to_thread`` 运行（不阻塞事件循环），payload 通过环境变量
``SYNAPSE_EVENT`` / ``SYNAPSE_PAYLOAD`` 传入。

ponytail: 先支持只读 PostToolUse 类钩子（告警/通知）；PreToolUse 阻断需接入
ActionAuthorizer 决策点，留作后续。钩子失败不影响主流程（best-effort）。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess


class HookRunner:
    def __init__(self, hooks: dict[str, list[str]] | None = None, timeout: float = 5.0):
        self._hooks = {k: list(v) for k, v in (hooks or {}).items() if v}
        self._timeout = timeout

    def attach(self, event_bus) -> None:
        """Subscribe a handler per configured event type."""
        for event_type, cmds in self._hooks.items():
            event_bus.subscribe(event_type, lambda e, cmds=cmds: self._run(e, cmds))

    async def _run(self, event, cmds: list[str]) -> None:
        payload = {k: v for k, v in vars(event).items() if not k.startswith("_")}
        env = dict(os.environ)
        env["SYNAPSE_EVENT"] = event.event_type
        env["SYNAPSE_PAYLOAD"] = json.dumps(payload, ensure_ascii=False, default=str)
        for cmd in cmds:
            try:
                await asyncio.to_thread(
                    subprocess.run, cmd, shell=True, timeout=self._timeout,
                    env=env, capture_output=True,
                )
            except Exception:
                # best-effort：钩子失败不能拖垮主流程
                pass
