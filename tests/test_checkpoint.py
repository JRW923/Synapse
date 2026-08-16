"""CheckpointManager — git snapshot semantics without touching user state."""

import subprocess
from pathlib import Path

import pytest

from synapse.modules.checkpoint import CheckpointManager


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "app.py").write_text("print('v1')\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_non_git_dir_is_unavailable(tmp_path: Path):
    mgr = CheckpointManager(tmp_path)
    assert mgr.available() is False
    assert mgr.create("x") is None
    assert mgr.list() == []


def test_restore_resets_tracked_files(repo: Path):
    mgr = CheckpointManager(repo)
    cp = mgr.create("baseline")
    assert cp is not None

    (repo / "app.py").write_text("print('broken v2')\n", encoding="utf-8")
    (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "wip")

    note = mgr.restore(cp)
    assert (repo / "app.py").read_text(encoding="utf-8") == "print('v1')\n"
    assert "restored" in note.lower()


def test_restore_keeps_post_checkpoint_untracked_files(repo: Path):
    mgr = CheckpointManager(repo)
    cp = mgr.create("baseline")
    (repo / "scratch.txt").write_text("agent junk\n", encoding="utf-8")
    mgr.restore(cp)
    # Deliberate: deleting untracked files would risk destroying user data.
    assert (repo / "scratch.txt").exists()


def test_snapshot_covers_untracked_files(repo: Path):
    mgr = CheckpointManager(repo)
    (repo / "agent_new.py").write_text("x = 1\n", encoding="utf-8")  # untracked
    cp = mgr.create("with-untracked")
    (repo / "agent_new.py").write_text("x = 2\n", encoding="utf-8")
    mgr.restore(cp)
    # add -A snapshot includes untracked files, so restore recovers them.
    assert (repo / "agent_new.py").read_text(encoding="utf-8") == "x = 1\n"


def test_user_index_and_staging_untouched(repo: Path):
    # Stage something deliberately before the checkpoint.
    (repo / "app.py").write_text("print('staged')\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    staged_before = _git(repo, "diff", "--cached", "--name-only")

    mgr = CheckpointManager(repo)
    assert mgr.create("task-start") is not None

    assert _git(repo, "diff", "--cached", "--name-only") == staged_before
    assert _git(repo, "status", "--porcelain").strip() == "M  app.py"


def test_restore_file_resets_one_file(repo: Path):
    mgr = CheckpointManager(repo)
    cp = mgr.create("baseline")
    (repo / "app.py").write_text("print('thrash')\n", encoding="utf-8")
    note = mgr.restore_file("app.py", cp)
    assert (repo / "app.py").read_text(encoding="utf-8") == "print('v1')\n"
    assert "reset" in note


def test_restore_file_removes_files_created_after_checkpoint(repo: Path):
    mgr = CheckpointManager(repo)
    cp = mgr.create("baseline")
    (repo / "gen.py").write_text("junk\n", encoding="utf-8")
    note = mgr.restore_file("gen.py", cp)
    assert not (repo / "gen.py").exists()
    assert "removed" in note


def test_list_is_ordered_and_latest_works(repo: Path):
    mgr = CheckpointManager(repo)
    mgr.create("first")
    mgr.create("second")
    cps = mgr.list()
    assert [c.label for c in cps] == ["0001-first", "0002-second"]
    assert mgr.latest().label == "0002-second"


def test_checkpoints_survive_across_manager_instances(repo: Path):
    # refs live in the repo, so --resume + /rewind work across processes.
    CheckpointManager(repo).create("persisted")
    assert CheckpointManager(repo).latest().label == "0001-persisted"


def test_empty_repo_headless_create(repo: Path):
    # A git repo with zero commits must still snapshot (commit-tree without -p).
    empty = repo / "sub"
    empty.mkdir()
    _git(empty, "init", "-q")
    _git(empty, "config", "user.email", "t@t")
    _git(empty, "config", "user.name", "t")
    (empty / "f.txt").write_text("hi\n", encoding="utf-8")
    mgr = CheckpointManager(empty)
    cp = mgr.create("headless")
    assert cp is not None
