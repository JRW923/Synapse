"""Tests for DBTool."""

import sqlite3
from pathlib import Path

import pytest

from synapse.modules.tools.db import DBTool


@pytest.fixture
def sample_db(tmp_path: Path) -> Path:
    """Create a temporary SQLite database with a sample table."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO users (id, name) VALUES (1, 'Alice')")
    conn.execute("INSERT INTO users (id, name) VALUES (2, 'Bob')")
    conn.commit()
    conn.close()
    return db_path


@pytest.mark.asyncio
async def test_select_query(sample_db: Path, tmp_path: Path):
    """SELECT queries should succeed and return expected rows."""
    tool = DBTool(workspace_root=str(tmp_path))
    result = await tool.execute({
        "db_path": str(sample_db),
        "query": "SELECT id, name FROM users ORDER BY id",
    })
    assert result.success
    assert "Alice" in result.output
    assert "Bob" in result.output


@pytest.mark.asyncio
async def test_write_blocked_by_default(sample_db: Path, tmp_path: Path):
    """INSERT/UPDATE/DELETE should fail when write mode is not enabled."""
    tool = DBTool(workspace_root=str(tmp_path))
    result = await tool.execute({
        "db_path": str(sample_db),
        "query": "INSERT INTO users (id, name) VALUES (3, 'Charlie')",
    })
    assert not result.success
    assert "write" in result.error.lower()


@pytest.mark.asyncio
async def test_write_allowed_when_enabled(sample_db: Path, tmp_path: Path):
    """INSERT should succeed when write=True."""
    tool = DBTool(workspace_root=str(tmp_path))
    result = await tool.execute({
        "db_path": str(sample_db),
        "query": "INSERT INTO users (id, name) VALUES (3, 'Charlie')",
        "write": True,
    })
    assert result.success

    # Verify the row was actually inserted
    result2 = await tool.execute({
        "db_path": str(sample_db),
        "query": "SELECT COUNT(*) FROM users",
    })
    assert "3" in result2.output


@pytest.mark.asyncio
async def test_error_on_invalid_sql(sample_db: Path, tmp_path: Path):
    """Malformed SQL should return an error result."""
    tool = DBTool(workspace_root=str(tmp_path))
    result = await tool.execute({
        "db_path": str(sample_db),
        "query": "SELEC * FROM nonexistent",
    })
    assert not result.success
    assert result.error is not None


@pytest.mark.asyncio
async def test_db_path_outside_workspace(tmp_path: Path):
    """Accessing a database outside the workspace should be blocked."""
    tool = DBTool(workspace_root=str(tmp_path))
    result = await tool.execute({
        "db_path": "/etc/passwd.db",
        "query": "SELECT 1",
    })
    assert not result.success
    assert "workspace" in result.error.lower()


@pytest.mark.asyncio
async def test_missing_db_file(tmp_path: Path):
    """Non-existent database file should return an error."""
    tool = DBTool(workspace_root=str(tmp_path))
    result = await tool.execute({
        "db_path": str(tmp_path / "nonexistent.db"),
        "query": "SELECT 1",
    })
    assert not result.success
    assert result.error is not None
