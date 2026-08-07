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


@pytest.mark.asyncio
async def test_ranking_prefers_relevant_file(tmp_path: Path, retriever, tools, memory):
    # The task keyword lives in one file's name + symbols; a decoy does not.
    (tmp_path / "payment_gateway.py").write_text(
        "def process_payment(amount):\n    return charge(amount)\n"
    )
    (tmp_path / "unrelated_math.py").write_text(
        "def add(a, b):\n    return a + b\n"
    )

    ctx = await retriever.retrieve(
        task="fix the process_payment charge bug",
        project_root=tmp_path,
        tools=tools,
        memory=memory,
    )

    file_blocks = [b for b in ctx.core if "# File:" in b.content]
    assert file_blocks, "expected at least one ranked file block"
    top = file_blocks[0].content
    assert "payment_gateway.py" in top
    # Python symbols must be extracted and surfaced in the header.
    assert "process_payment" in top


@pytest.mark.asyncio
async def test_tree_block_lists_files(tmp_path: Path, retriever, tools, memory):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")

    ctx = await retriever.retrieve(
        task="anything", project_root=tmp_path, tools=tools, memory=memory,
    )

    tree = [b for b in ctx.core if "# Project files" in b.content]
    assert tree, "expected a file-tree overview block"
    assert "a.py" in tree[0].content and "b.py" in tree[0].content
