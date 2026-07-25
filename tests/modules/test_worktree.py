"""Tests for s18 Worktree 隔离 (WorktreeManager + Swarm 接入).

验收：两个 worker 各写同名文件到各自 worktree，互不污染；结束后清理。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from synapse.modules.planning.worktree import WorktreeManager, WorktreeError
from synapse.modules.planning.swarm import SwarmPlanner, RoleSpec
from synapse.modules.security.auth import ActionAuthorizer
from synapse.protocols.planner import ResultStatus, PlanningMode
from synapse.protocols.llm import LLMResponse
from synapse.core.events import EventBus
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _write(path: Path, name: str, content: str) -> None:
    (path / name).write_text(content, encoding="utf-8")


def test_non_git_isolation_and_cleanup(tmp_path: Path):
    """非 git 目录：两个 worktree 互不污染且结束后被清理。"""
    mgr = WorktreeManager(tmp_path)
    a = mgr.create("worker-a")
    b = mgr.create("worker-b")

    assert a != b
    _write(a, "result.txt", "from A")
    _write(b, "result.txt", "from B")

    assert (a / "result.txt").read_text() == "from A"
    assert (b / "result.txt").read_text() == "from B"
    # 互不影响
    assert (b / "result.txt").read_text() != "from A"

    mgr.remove_all()
    assert "worker-a" not in mgr
    assert "worker-b" not in mgr
    assert not a.exists() and not b.exists()


def test_git_worktree_isolation_and_cleanup(tmp_path: Path):
    """git 仓库：用 git worktree 隔离，清理后目录与分支均移除。"""
    r = subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    # 需要一个初始 commit，worktree 才能从 HEAD 分支
    (tmp_path / "seed.txt").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), capture_output=True, text=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(tmp_path), capture_output=True, text=True)

    mgr = WorktreeManager(tmp_path)
    a = mgr.create("worker-a")
    assert a.exists()
    # worktree 是独立的工作副本（含 .git 指向主仓库）
    assert (a / "seed.txt").exists()

    _write(a, "result.txt", "from A")
    assert (tmp_path / "result.txt").exists() is False  # 不污染主工作区

    mgr.remove("worker-a")
    assert not a.exists()
    branches = subprocess.run(
        ["git", "branch", "--list", "synapse-worker-a"],
        cwd=str(tmp_path), capture_output=True, text=True,
    ).stdout
    assert "synapse-worker-a" not in branches


def test_swarm_creates_and_cleans_worktrees(tmp_path: Path):
    """Swarm 接入：file-scoped coder 各自拿到 worktree，execute 结束后全部清理。"""
    base_auth = ActionAuthorizer(workspace_root=str(tmp_path), confirmation_enabled=True)
    base = SimpleNamespace(
        auth=base_auth,
        max_iterations=10, thrashing_threshold=3, max_thrashing_events=3,
        max_tokens_per_task=1000, _confirm=None, total_timeout_seconds=120,
    )
    roles = [RoleSpec(role="coder", file_scope=True, count=2)]
    wt = WorktreeManager(tmp_path / "wt")

    # decompose (1) + merge (1) = 2 LLM 调用；两 coder 各自一次
    llm = AsyncMock()
    llm.chat.side_effect = [
        LLMResponse('[{"id":"1","description":"a","file_scope":"src/a.py"},'
                    '{"id":"2","description":"b","file_scope":"src/b.py"}]'),
        LLMResponse("merged"),
    ]

    async def coder_run(**kw):
        return type("R", (), {"status": ResultStatus.SUCCESS, "output": "ok",
                              "metrics": SimpleNamespace(
                                  tokens_input=0, tokens_output=0, tool_call_count=0,
                                  tool_success_count=0, thrashing_events=0)})()

    def factory(spec):
        p = MagicMock()
        p.execute = AsyncMock(side_effect=coder_run)
        p.mode = PlanningMode.REACT
        return p

    planner = SwarmPlanner(react_planner=base, roles=roles, worktree_manager=wt, planner_factory=factory)

    bus = EventBus()
    session = MagicMock()
    session.id = "sess"
    session.fork.return_value = MagicMock()

    async def _run():
        return await planner.execute(
            task="实现功能", context=SimpleNamespace(), tools=MagicMock(),
            llm=llm, sandbox=MagicMock(), session=session, event_bus=bus,
        )

    import asyncio
    asyncio.run(_run())

    # 所有 worktree 已清理
    assert len(wt._paths) == 0
    assert not any(wt.worktrees_dir.iterdir())
