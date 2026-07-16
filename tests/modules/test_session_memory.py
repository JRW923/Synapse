"""Tests for session memory."""
import pytest
from synapse.modules.memory.session import SessionMemory
from synapse.protocols.memory import MemoryEntry, MemoryLevel, MemoryMetadata


@pytest.mark.asyncio
async def test_store_and_retrieve_session():
    mem = SessionMemory()
    entry = MemoryEntry(
        id="e1",
        content="This is a session memory",
        level=MemoryLevel.SESSION,
        metadata=MemoryMetadata(tags=["test"]),
    )
    await mem.store(entry)
    results = await mem.retrieve("session", MemoryLevel.SESSION, top_k=5)
    assert len(results) == 1
    assert results[0].content == "This is a session memory"


@pytest.mark.asyncio
async def test_forget():
    mem = SessionMemory()
    entry = MemoryEntry(id="e1", content="temp", level=MemoryLevel.SESSION)
    await mem.store(entry)
    await mem.forget("e1")
    results = await mem.retrieve("temp", MemoryLevel.SESSION)
    assert len(results) == 0


@pytest.mark.asyncio
async def test_retrieve_other_level_returns_empty():
    mem = SessionMemory()
    entry = MemoryEntry(id="e1", content="session only", level=MemoryLevel.SESSION)
    await mem.store(entry)
    results = await mem.retrieve("session", MemoryLevel.PROJECT)
    assert len(results) == 0


@pytest.mark.asyncio
async def test_multiple_entries():
    mem = SessionMemory()
    for i in range(10):
        await mem.store(MemoryEntry(id=f"e{i}", content=f"entry {i}", level=MemoryLevel.SESSION))
    results = await mem.retrieve("entry", MemoryLevel.SESSION, top_k=3)
    assert len(results) <= 3
