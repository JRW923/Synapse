"""Git-backed checkpoints for long-running tasks.

A checkpoint is a git tree snapshot of the whole workspace, written through a
temporary index so the user's staging area and working tree are never touched.
Snapshots live under ``refs/synapse/checkpoints/`` — a real ref namespace,
which means they survive process restarts (``--resume`` + ``/rewind`` work
across sessions) and are immune to ``git gc`` pruning.

Restore semantics: tracked files are reset to the checkpoint tree. Untracked
files created after the checkpoint are deliberately left alone — deleting them
would risk destroying user data we cannot attribute to the agent.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

_REF_PREFIX = "refs/synapse/checkpoints"
_SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass
class Checkpoint:
    ref: str        # full ref, e.g. refs/synapse/checkpoints/0003-label
    tree: str       # tree sha
    label: str
    timestamp: str  # committer date, ISO-ish from for-each-ref


class CheckpointManager:
    """Create and restore workspace checkpoints in a git repository."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._available: bool | None = None  # probed once, then cached

    # ------------------------------------------------------------------
    def _git(self, args: list[str], env_extra: dict | None = None,
             check: bool = True) -> subprocess.CompletedProcess:
        env = {**os.environ, **(env_extra or {})}
        return subprocess.run(
            ["git", *args], cwd=str(self.root), capture_output=True,
            text=True, encoding="utf-8", errors="replace", env=env,
            **({"check": True} if check else {}),
        )

    def available(self) -> bool:
        if self._available is None:
            try:
                r = self._git(["rev-parse", "--is-inside-work-tree"], check=False)
                self._available = (r.returncode == 0 and r.stdout.strip() == "true")
            except FileNotFoundError:
                self._available = False
        return self._available

    # ------------------------------------------------------------------
    def create(self, label: str = "") -> Checkpoint | None:
        """Snapshot the workspace (tracked + untracked) without touching it.

        Returns None (no exception) when this is not a git repo — callers
        treat checkpoints as a best-effort capability.
        """
        if not self.available():
            return None
        safe = _SAFE_LABEL.sub("-", label)[:40].strip("-") or "checkpoint"
        n = len(self.list()) + 1
        ref = f"{_REF_PREFIX}/{n:04d}-{safe}"

        # Build the snapshot with a throwaway index copied from the real one,
        # so staged state is preserved in the snapshot and the user's index
        # is left exactly as it was.
        index_backup = tempfile.NamedTemporaryFile(
            delete=False, suffix=".synapse-index")
        index_backup.close()
        try:
            git_dir = Path(self._git(["rev-parse", "--git-dir"]).stdout.strip())
            if not git_dir.is_absolute():
                git_dir = self.root / git_dir
            source = git_dir / "index"
            if source.exists():
                self._cat(source, index_backup.name)
            else:
                # Empty repo, no index yet: a 0-byte file reads as a corrupt
                # index, so let git create a fresh one at this path.
                os.unlink(index_backup.name)
            env = {"GIT_INDEX_FILE": index_backup.name}
            self._git(["add", "-A"], env_extra=env)
            tree = self._git(["write-tree"], env_extra=env).stdout.strip()

            head = self._git(["rev-parse", "--verify", "HEAD"], check=False)
            parents = ["-p", head.stdout.strip()] if head.returncode == 0 else []
            commit = self._git(
                ["commit-tree", tree, *parents, "-m", f"synapse checkpoint: {safe}"],
            ).stdout.strip()
            self._git(["update-ref", ref, commit])
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None
        finally:
            Path(index_backup.name).unlink(missing_ok=True)

        from datetime import datetime
        return Checkpoint(ref=ref, tree=tree, label=safe,
                          timestamp=datetime.now().isoformat(timespec="seconds"))

    @staticmethod
    def _cat(src: Path, dst: str) -> None:
        data = src.read_bytes()
        with open(dst, "wb") as f:
            f.write(data)

    # ------------------------------------------------------------------
    def list(self) -> list[Checkpoint]:
        """All checkpoints, oldest first (ref names are zero-padded counters)."""
        if not self.available():
            return []
        r = self._git([
            "for-each-ref", _REF_PREFIX,
            "--format=%(refname)|%(tree)|%(committerdate:iso8601)",
        ], check=False)
        out = []
        for line in r.stdout.splitlines():
            parts = line.split("|", maxsplit=2)
            if len(parts) != 3 or not parts[1]:
                continue
            ref, tree, ts = parts
            label = ref.rsplit("/", 1)[1]
            out.append(Checkpoint(ref=ref, tree=tree, label=label, timestamp=ts))
        out.sort(key=lambda c: c.ref)
        return out

    def latest(self) -> Checkpoint | None:
        cps = self.list()
        return cps[-1] if cps else None

    # ------------------------------------------------------------------
    def restore(self, checkpoint: Checkpoint) -> str:
        """Reset tracked files to the checkpoint tree. Returns a status note.

        Untracked files created after the checkpoint are intentionally kept.
        """
        self._git(["read-tree", "-u", "--reset", checkpoint.tree])
        return (f"Workspace restored to checkpoint {checkpoint.label}. "
                f"Untracked files created after the checkpoint were left in place.")

    def restore_file(self, path: str, checkpoint: Checkpoint) -> str:
        """Reset one file to its checkpoint state (deleted if the checkpoint
        predates it). Used by thrashing recovery so the model gets one clean
        retry instead of stacking a 4th edit on 3 failed ones."""
        rel = str(Path(path).relative_to(self.root)) if Path(path).is_absolute() else path
        in_tree = self._git(
            ["ls-tree", "--name-only", checkpoint.tree, "--", rel], check=False
        ).stdout.strip()
        if in_tree:
            self._git(["checkout", checkpoint.tree, "--", rel])
            return f"{rel} reset to checkpoint state"
        # File did not exist at checkpoint time: remove it so the retry starts clean.
        target = self.root / rel
        if target.exists():
            target.unlink()
        return f"{rel} removed (did not exist at checkpoint)"
