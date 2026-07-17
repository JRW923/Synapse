"""Tests for user memory — cross-project persistent storage in ~/.synapse/memory/."""

from pathlib import Path

import pytest

from synapse.modules.memory.user import UserMemory
from synapse.protocols.memory import MemoryEntry, MemoryLevel, MemoryMetadata


@pytest.fixture
def user_memory(tmp_path, monkeypatch):
    """Return a UserMemory whose home directory is redirected to tmp_path."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return UserMemory()


@pytest.mark.asyncio
async def test_store_and_retrieve_user(user_memory):
    """A USER-level entry can be stored and later retrieved by query."""
    entry = MemoryEntry(
        id="user-1",
        content="Remember to use strict type annotations in all new code.",
        level=MemoryLevel.USER,
        metadata=MemoryMetadata(tags=["style", "python"]),
    )
    await user_memory.store(entry)

    results = await user_memory.retrieve("type annotations", MemoryLevel.USER, top_k=5)
    assert len(results) == 1
    assert results[0].id == "user-1"
    assert results[0].content == entry.content
    assert results[0].level == MemoryLevel.USER
    assert set(results[0].metadata.tags) == {"style", "python"}


@pytest.mark.asyncio
async def test_cross_project_persistence(user_memory):
    """User memory persists across different project paths — it lives in ~/.synapse/memory/,
    independent of any single project."""
    entry = MemoryEntry(
        id="cross-1",
        content="Global user preference: always use double quotes.",
        level=MemoryLevel.USER,
        metadata=MemoryMetadata(tags=["global"]),
    )
    await user_memory.store(entry)

    # Simulate retrieval from a completely different "project" — same UserMemory
    # instance already points to the same fake home, so data should still be there.
    results = await user_memory.retrieve("double quotes", MemoryLevel.USER, top_k=5)
    assert len(results) == 1
    assert results[0].id == "cross-1"

    # Also verify the underlying file exists in the expected directory.
    memory_dir = Path.home() / ".synapse" / "memory"
    expected_file = memory_dir / "cross-1.md"
    assert expected_file.exists()
    assert expected_file.read_text(encoding="utf-8").strip() != ""


@pytest.mark.asyncio
async def test_retrieve_wrong_level_returns_empty(user_memory):
    """retrieve() with a non-USER level returns an empty list, even when USER entries exist."""
    entry = MemoryEntry(
        id="user-only",
        content="This is a user-scoped note.",
        level=MemoryLevel.USER,
    )
    await user_memory.store(entry)

    for wrong_level in (MemoryLevel.SESSION, MemoryLevel.PROJECT, MemoryLevel.SEMANTIC):
        results = await user_memory.retrieve("user-scoped", wrong_level, top_k=5)
        assert results == [], f"Expected empty list for level {wrong_level}"
