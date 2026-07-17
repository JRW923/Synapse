"""Tests for project memory."""
import pytest
from synapse.modules.memory.project import ProjectMemory
from synapse.protocols.memory import MemoryEntry, MemoryLevel, MemoryMetadata


@pytest.mark.asyncio
async def test_store_and_retrieve(tmp_path):
    """Store an entry and retrieve it by query substring."""
    mem = ProjectMemory(base_path=tmp_path / ".synapse" / "memory")

    entry = MemoryEntry(
        id="arch-1",
        content="The project uses a layered architecture with protocols, modules, and adapters.",
        level=MemoryLevel.PROJECT,
        metadata=MemoryMetadata(tags=["architecture", "design"], priority=8),
    )
    await mem.store(entry)

    results = await mem.retrieve("layered architecture", MemoryLevel.PROJECT, top_k=5)
    assert len(results) == 1
    assert results[0].id == "arch-1"
    assert results[0].metadata.priority == 8


@pytest.mark.asyncio
async def test_forget(tmp_path):
    """Forget removes an entry so it is no longer retrievable."""
    mem = ProjectMemory(base_path=tmp_path / ".synapse" / "memory")

    entry = MemoryEntry(
        id="conv-1",
        content="Use async/await for all I/O operations.",
        level=MemoryLevel.PROJECT,
        metadata=MemoryMetadata(tags=["conventions"], priority=5),
    )
    await mem.store(entry)

    # Verify it exists
    results = await mem.retrieve("async/await", MemoryLevel.PROJECT)
    assert len(results) == 1

    await mem.forget("conv-1")

    results = await mem.retrieve("async/await", MemoryLevel.PROJECT)
    assert len(results) == 0


@pytest.mark.asyncio
async def test_retrieve_wrong_level_returns_empty(tmp_path):
    """Retrieving with a non-PROJECT level returns an empty list."""
    mem = ProjectMemory(base_path=tmp_path / ".synapse" / "memory")

    entry = MemoryEntry(
        id="proj-1",
        content="Project-level only memory.",
        level=MemoryLevel.PROJECT,
        metadata=MemoryMetadata(tags=["general"], priority=5),
    )
    await mem.store(entry)

    # Should only retrieve at PROJECT level
    results = await mem.retrieve("memory", MemoryLevel.SESSION)
    assert len(results) == 0

    results = await mem.retrieve("memory", MemoryLevel.USER)
    assert len(results) == 0


@pytest.mark.asyncio
async def test_memory_directory_auto_created(tmp_path):
    """The memory directory is created automatically on first store."""
    mem = ProjectMemory(base_path=tmp_path / ".synapse" / "memory")

    # Directory should not exist yet
    assert not mem._base_path.exists()

    entry = MemoryEntry(
        id="arch-1",
        content="Layered architecture.",
        level=MemoryLevel.PROJECT,
        metadata=MemoryMetadata(tags=["architecture"], priority=5),
    )
    await mem.store(entry)

    # Directory should now exist
    assert mem._base_path.exists()
    assert mem._base_path.is_dir()
