"""Phase 4 integration tests — external tools (HTTP/DB/Browser) wiring
and Qdrant memory backend through the Synapse facade.
"""

import sqlite3
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from synapse.protocols.llm import LLMResponse
from synapse.protocols.memory import MemoryEntry, MemoryLevel, MemoryMetadata
from synapse.protocols.tool import Tool, ToolRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_llm(content: str = "Task completed.") -> AsyncMock:
    """Return a mock LLM provider that returns *content*."""
    mock = AsyncMock()
    mock.model_id = "mock"
    mock.chat.return_value = LLMResponse(
        content=content,
        tool_calls=[],
        stop_reason="end_turn",
        usage={"input": 10, "output": 5},
    )
    return mock


# ---------------------------------------------------------------------------
# Test 1: HTTPTool registered and callable via tool registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_tool_in_pipeline(httpx_mock):
    """HTTPTool is registered when enable_external_tools=True and can be
    called via the tool registry directly."""
    mock_llm = _make_mock_llm()

    with patch(
        "synapse.modules.providers.anthropic.AnthropicProvider",
        return_value=mock_llm,
    ):
        from synapse.adapters.library import Synapse

        synapse = Synapse(
            provider="anthropic",
            enable_external_tools=True,
        )

    # Resolve the tool registry from the container
    registry: ToolRegistry = synapse._container.resolve(ToolRegistry)

    # HTTPTool should be registered
    http_tool = None
    for tool in registry.list_all():
        if tool.name == "web":
            http_tool = tool
            break
    assert http_tool is not None, "HTTPTool ('web') should be registered when enable_external_tools=True"

    # Verify it can be called
    httpx_mock.add_response(
        url="https://httpbin.org/get",
        method="GET",
        text='{"url": "https://httpbin.org/get"}',
        status_code=200,
    )

    result = await http_tool.execute({"url": "https://httpbin.org/get", "method": "GET"})
    assert result.success
    assert "httpbin.org/get" in result.output


# ---------------------------------------------------------------------------
# Test 2: DBTool executes SELECT on a temporary database
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_tool_select(tmp_path: Path):
    """DBTool is registered when enable_external_tools=True and can execute
    a SELECT query against a temporary SQLite database."""
    # Create a temp database
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO users (id, name) VALUES (1, 'Alice')")
    conn.execute("INSERT INTO users (id, name) VALUES (2, 'Bob')")
    conn.commit()
    conn.close()

    mock_llm = _make_mock_llm()

    with patch(
        "synapse.modules.providers.anthropic.AnthropicProvider",
        return_value=mock_llm,
    ):
        from synapse.adapters.library import Synapse

        synapse = Synapse(
            provider="anthropic",
            enable_external_tools=True,
            workspace_root=str(tmp_path),
        )

    # Resolve the tool registry from the container
    registry: ToolRegistry = synapse._container.resolve(ToolRegistry)

    # DBTool should be registered
    db_tool = None
    for tool in registry.list_all():
        if tool.name == "db":
            db_tool = tool
            break
    assert db_tool is not None, "DBTool should be registered when enable_external_tools=True"

    # Execute a SELECT query
    result = await db_tool.execute({
        "db_path": str(db_path),
        "query": "SELECT id, name FROM users ORDER BY id",
    })
    assert result.success
    assert "Alice" in result.output
    assert "Bob" in result.output


# ---------------------------------------------------------------------------
# Test 3: External tools are disabled by default
# ---------------------------------------------------------------------------


def test_external_tools_disabled_by_default():
    """When enable_external_tools is False (default), no EXTERNAL-risk tools
    (HTTPTool, DBTool, BrowserTool) are registered in the ToolRegistry."""
    mock_llm = _make_mock_llm()

    with patch(
        "synapse.modules.providers.anthropic.AnthropicProvider",
        return_value=mock_llm,
    ):
        from synapse.adapters.library import Synapse

        synapse = Synapse(provider="anthropic")  # enable_external_tools defaults to False

    registry: ToolRegistry = synapse._container.resolve(ToolRegistry)

    tool_names = {t.name for t in registry.list_all()}

    # Only built-in tools should be present
    assert "read" in tool_names, "Built-in read tool missing"
    assert "write" in tool_names, "Built-in write tool missing"
    assert "edit" in tool_names, "Built-in edit tool missing"
    assert "glob" in tool_names, "Built-in glob tool missing"
    assert "grep" in tool_names, "Built-in grep tool missing"
    assert "shell" in tool_names, "Built-in shell tool missing"
    assert "git" in tool_names, "Built-in git tool missing"

    # External tools must NOT be registered
    assert "web" not in tool_names, "HTTPTool should NOT be registered by default"
    assert "db" not in tool_names, "DBTool should NOT be registered by default"
    assert "browser" not in tool_names, "BrowserTool should NOT be registered by default"


# ---------------------------------------------------------------------------
# Test 4: Qdrant memory backend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qdrant_memory_backend():
    """Synapse(memory_backend=\"qdrant\") should wire QdrantMemory into the
    container and support store/retrieve of SEMANTIC-level entries."""
    mock_llm = _make_mock_llm()

    with patch(
        "synapse.modules.providers.anthropic.AnthropicProvider",
        return_value=mock_llm,
    ):
        from synapse.adapters.library import Synapse

        synapse = Synapse(
            provider="anthropic",
            memory_backend="qdrant",
        )

    # Resolve the memory store from the container
    from synapse.protocols.memory import MemoryStore as MSProto
    memory_store = synapse._container.resolve(MSProto)
    assert memory_store is not None

    # Store a semantic memory entry
    entry_id = str(uuid.uuid4())
    entry = MemoryEntry(
        id=entry_id,
        content="The authentication module uses JWT tokens with RS256",
        level=MemoryLevel.SEMANTIC,
        metadata=MemoryMetadata(
            tags=["auth", "security", "jwt"],
            priority=8,
            project="synapse",
        ),
    )
    await memory_store.store(entry)

    # Retrieve by semantic similarity
    results = await memory_store.retrieve(
        "How does authentication work?",
        MemoryLevel.SEMANTIC,
        top_k=3,
    )

    assert len(results) > 0, "Should retrieve at least one semantic match"
    retrieved_ids = [r.id for r in results]
    assert entry_id in retrieved_ids, (
        f"Stored entry {entry_id} not found in results {retrieved_ids}"
    )

    # Verify content is preserved
    retrieved = next(r for r in results if r.id == entry_id)
    assert retrieved.level == MemoryLevel.SEMANTIC
    assert "JWT tokens" in retrieved.content
    assert "auth" in retrieved.metadata.tags

    # Clean up
    await memory_store.forget(entry_id)
