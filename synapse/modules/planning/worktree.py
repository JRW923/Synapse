"""Worktree 隔离 — 为并行 worker 提供文件系统隔离 (s18).

ponytail: 仅支持 git 仓库；非 git 目录退化为独立临时子目录（best-effort 隔离，
仍互不污染）。所有 worktree 统一放在 ``<base_root>/.synapse/worktrees/<agent_id>``。
本模块只负责「创建/清理」隔离目录，不负责把结果合并回主干——合并是上层
（s17 任务板 + 显式 merge 步骤）的职责，留作后续。
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path


class WorktreeError(Exception):
    """Raised when a git worktree operation fails."""


class WorktreeManager:
    """Create per-worker isolated directories on top of a base workspace.

    - git repo → ``git worktree add`` (real branch isolation).
    - non-git → ``tempfile``-style independent subdir under ``.synapse/worktrees``.
    """

    def __init__(self, base_root: str | Path, worktrees_dir: str | Path | None = None):
        self.base_root = Path(base_root).resolve()
        self.worktrees_dir = (
            Path(worktrees_dir).resolve()
            if worktrees_dir
            else self.base_root / ".synapse" / "worktrees"
        )
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self._paths: dict[str, Path] = {}
        self._snapshots: dict[str, dict[str, str]] = {}
        self._merged_hashes: dict[str, str] = {}
        self._conflicts: list[str] = []

    # ------------------------------------------------------------------
    def _is_git_repo(self) -> bool:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(self.base_root),
                capture_output=True, text=True,
            )
            return r.returncode == 0 and r.stdout.strip() == "true"
        except FileNotFoundError:
            return False

    def create(self, agent_id: str) -> Path:
        """Create (or reuse) an isolated dir for *agent_id*; return its path."""
        path = self.worktrees_dir / agent_id
        if self._is_git_repo():
            branch = f"synapse-{agent_id}"
            try:
                subprocess.run(
                    ["git", "worktree", "add", "-b", branch, str(path)],
                    cwd=str(self.base_root),
                    capture_output=True, text=True, check=True,
                )
            except subprocess.CalledProcessError as e:
                raise WorktreeError(f"git worktree add failed: {e.stderr}") from e
        else:
            path.mkdir(parents=True, exist_ok=True)
        self._paths[agent_id] = path
        self._snapshots[agent_id] = self._snapshot(path)
        return path

    def remove(self, agent_id: str) -> None:
        """Remove the isolated dir (and its git branch, if any) for *agent_id*."""
        path = self._paths.pop(agent_id, None)
        self._snapshots.pop(agent_id, None)
        if path is None:
            return
        if self._is_git_repo() and (path / ".git").exists() or self._has_git_worktree(path):
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(path)],
                    cwd=str(self.base_root),
                    capture_output=True, text=True, check=True,
                )
                subprocess.run(
                    ["git", "branch", "-D", f"synapse-{agent_id}"],
                    cwd=str(self.base_root),
                    capture_output=True, text=True,
                )
            except subprocess.CalledProcessError:
                if path.exists():
                    shutil.rmtree(path, ignore_errors=True)
        else:
            shutil.rmtree(path, ignore_errors=True)

    def merge_back(self, agent_id: str) -> list[str]:
        """Copy a worker's worktree contents back into the base workspace.

        ponytail: a real git merge is left for later (see module docstring).
        Until then, a best-effort file copy is the only thing preventing the
        swarm's ``finally: remove_all()`` from silently discarding every
        worker's edits. Top-level entries mirror the checkout, so copying them
        (skipping ``.git``) folds worker changes into the base workspace.
        """
        path = self._paths.get(agent_id)
        if path is None or not path.exists():
            return []
        baseline = self._snapshots.get(agent_id, {})
        conflicts: list[str] = []
        for rel, digest in self._snapshot(path).items():
            if baseline.get(rel) == digest:
                continue
            source = path / rel
            dest = self.base_root / rel
            previous = self._merged_hashes.get(rel)
            if previous is not None and previous != digest:
                conflicts.append(rel)
                self._conflicts.append(f"{agent_id}:{rel}")
                continue
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
                self._merged_hashes[rel] = digest
            except OSError:
                # Best-effort: skip files we can't copy rather than abort cleanup.
                continue
        return conflicts

    def merge_all(self) -> list[str]:
        """Merge every live worktree back into the base workspace."""
        conflicts: list[str] = []
        for agent_id in list(self._paths):
            conflicts.extend(self.merge_back(agent_id))
        return conflicts

    @property
    def conflicts(self) -> list[str]:
        return list(self._conflicts)

    @staticmethod
    def _snapshot(root: Path) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        for path in root.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            snapshot[path.relative_to(root).as_posix()] = digest
        return snapshot

    @staticmethod
    def _has_git_worktree(path: Path) -> bool:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(path), capture_output=True, text=True,
            )
            return r.returncode == 0 and r.stdout.strip() == "true"
        except (FileNotFoundError, OSError):
            return False

    def remove_all(self) -> None:
        for agent_id in list(self._paths):
            self.remove(agent_id)

    def __contains__(self, agent_id: str) -> bool:
        return agent_id in self._paths
