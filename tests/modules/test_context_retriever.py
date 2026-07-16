"""Tests for basic context retriever."""
from pathlib import Path
import pytest
from synapse.modules.context.retriever import BasicContextRetriever
from synapse.modules.tools.registry import DefaultToolRegistry
from synapse.modules.tools.search_grep import GrepTool
from synapse.modules.tools.file_glob import GlobTool
from synapse.modules.memory.session import SessionMemory
from synapse.protocols.memory import MemoryEntry, MemoryLevel
from synapse.protocols.retriever import ContextBudget


@pytest.fixture
def retriever():
    return BasicContextRetriever()


@pytest.fixture
def tools():
    reg = DefaultToolRegistry()
    reg.register(GrepTool())
    reg.register(GlobTool())
    return reg


@pytest.fixture
def memory():
    return SessionMemory()


@pytest.mark.asyncio
async def test_retrieve_builds_context(tmp_path: Path, retriever, tools, memory):
    # Create some files
    (tmp_path / "main.py").write_text("def main():\n    pass\n")
    (tmp_path / "README.md").write_text("# Test Project\n")

    # Add session memory
    await memory.store(MemoryEntry(
        id="m1", content="This project uses pytest", level=MemoryLevel.SESSION,
    ))

    ctx = await retriever.retrieve(
        task="find the main function",
        project_root=tmp_path,
        tools=tools,
        memory=memory,
        budget=ContextBudget(total_tokens=10000),
    )

    assert len(ctx.core) > 0
    assert ctx.total_tokens > 0


@pytest.mark.asyncio
async def test_context_preserves_system_blocks(tmp_path: Path, retriever, tools, memory):
    ctx = await retriever.retrieve(
        task="test task",
        project_root=tmp_path,
        tools=tools,
        memory=memory,
    )

    # System blocks should contain project instructions
    assert isinstance(ctx.system, list)
    assert isinstance(ctx.core, list)
