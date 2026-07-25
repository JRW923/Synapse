"""TaskBoard — 可观察、可认领的任务看板 (s12 + s17).

既服务单 agent（HierarchicalPlanner 串行认领自身子任务，s12），也服务 Swarm
（多个通用 worker 并发认领，s17）。状态变更发事件，供 REPL/CLI 实时展示。

ponytail: 进程内内存看板（dict + asyncio.Lock），无持久化；多进程需外部存储
（s14 Cron 调度可接入同一接口）。``claim`` 在锁内完成原子取任务，保证并发
worker 不会重复认领同一任务。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum

from synapse.protocols.events import (
    TaskStatusChanged, TaskClaimed, TaskReleased,
)


class TaskStatus(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    DONE = "done"


@dataclass
class BoardTask:
    id: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    owner: str = ""
    result: object = None
    meta: dict = field(default_factory=dict)


class TaskBoard:
    def __init__(self, event_bus=None, session_id: str = ""):
        self._tasks: dict[str, BoardTask] = {}
        self._lock = asyncio.Lock()
        self._event_bus = event_bus
        self._session_id = session_id

    # ------------------------------------------------------------------
    async def add(self, task_id: str, description: str, **meta) -> None:
        self._tasks[task_id] = BoardTask(id=task_id, description=description, meta=meta)
        await self._emit_status(task_id, TaskStatus.PENDING, "")

    async def add_many(self, items: list[tuple[str, str]]) -> None:
        for tid, desc in items:
            await self.add(tid, desc)

    async def claim(self, worker_id: str) -> BoardTask | None:
        """Atomically claim a pending task for *worker_id*; None if none left."""
        async with self._lock:
            for t in self._tasks.values():
                if t.status is TaskStatus.PENDING:
                    t.status = TaskStatus.CLAIMED
                    t.owner = worker_id
                    await self._emit_status(t.id, TaskStatus.CLAIMED, worker_id)
                    if self._event_bus is not None:
                        await self._event_bus.emit(TaskClaimed(
                            session_id=self._session_id, agent_id=worker_id,
                            task_id=t.id, owner=worker_id,
                        ))
                    return t
        return None

    async def complete(self, task_id: str, result: object = None) -> None:
        async with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            t.status = TaskStatus.DONE
            t.result = result
            await self._emit_status(task_id, TaskStatus.DONE, t.owner)

    async def release(self, task_id: str) -> None:
        async with self._lock:
            t = self._tasks.get(task_id)
            if t is None or t.status is not TaskStatus.CLAIMED:
                return
            owner = t.owner
            t.status = TaskStatus.PENDING
            t.owner = ""
            await self._emit_status(task_id, TaskStatus.PENDING, owner)
            if self._event_bus is not None:
                await self._event_bus.emit(TaskReleased(
                    session_id=self._session_id, agent_id=owner,
                    task_id=task_id, owner=owner,
                ))

    # ------------------------------------------------------------------
    def pending(self) -> list[BoardTask]:
        return [t for t in self._tasks.values() if t.status is TaskStatus.PENDING]

    def claimed_by(self, worker_id: str) -> list[BoardTask]:
        return [t for t in self._tasks.values() if t.owner == worker_id]

    def all_done(self) -> bool:
        return bool(self._tasks) and all(t.status is TaskStatus.DONE for t in self._tasks.values())

    def __len__(self) -> int:
        return len(self._tasks)

    def __contains__(self, task_id: str) -> bool:
        return task_id in self._tasks

    # ------------------------------------------------------------------
    async def _emit_status(self, task_id: str, status: TaskStatus, owner: str) -> None:
        if self._event_bus is None:
            return
        await self._event_bus.emit(TaskStatusChanged(
            session_id=self._session_id, agent_id=owner,
            task_id=task_id, status=status.value, owner=owner,
        ))
