"""Tests for basic tools."""
import os
import tempfile
from pathlib import Path
import pytest
from synapse.modules.tools.file_read import ReadTool
from synapse.modules.tools.file_write import WriteTool
from synapse.modules.tools.file_edit import EditTool
from synapse.modules.tools.file_glob import GlobTool
from synapse.modules.tools.search_grep import GrepTool


@pytest.mark.asyncio
async def test_read_tool(tmp_path: Path):
    f = tmp_path / "test.txt"
    f.write_text("hello world")
    tool = ReadTool()
    result = await tool.execute({"path": str(f)})
    assert result.success
    assert "hello world" in result.output


@pytest.mark.asyncio
async def test_read_tool_nonexistent():
    tool = ReadTool()
    result = await tool.execute({"path": "/nonexistent/file.txt"})
    assert not result.success


@pytest.mark.asyncio
async def test_write_tool(tmp_path: Path):
    f = tmp_path / "output.txt"
    tool = WriteTool()
    result = await tool.execute({"path": str(f), "content": "new content"})
    assert result.success
    assert f.read_text() == "new content"


@pytest.mark.asyncio
async def test_edit_tool(tmp_path: Path):
    f = tmp_path / "edit.txt"
    f.write_text("line1\nline2\nline3\n")
    tool = EditTool()
    result = await tool.execute({
        "path": str(f),
        "old_string": "line2\n",
        "new_string": "replaced\n",
    })
    assert result.success
    assert f.read_text() == "line1\nreplaced\nline3\n"


@pytest.mark.asyncio
async def test_edit_tool_not_unique(tmp_path: Path):
    f = tmp_path / "dup.txt"
    f.write_text("dup\ndup\n")
    tool = EditTool()
    result = await tool.execute({
        "path": str(f),
        "old_string": "dup\n",
        "new_string": "x\n",
    })
    assert not result.success
    assert "not unique" in result.error.lower()


@pytest.mark.asyncio
async def test_glob_tool(tmp_path: Path):
    (tmp_path / "a.py").touch()
    (tmp_path / "b.py").touch()
    (tmp_path / "c.txt").touch()
    tool = GlobTool()
    result = await tool.execute({"pattern": "*.py", "path": str(tmp_path)})
    assert result.success
    assert "a.py" in result.output
    assert "b.py" in result.output
    assert "c.txt" not in result.output


@pytest.mark.asyncio
async def test_grep_tool(tmp_path: Path):
    (tmp_path / "src.py").write_text("def foo():\n    pass\ndef bar():\n    pass\n")
    tool = GrepTool()
    result = await tool.execute({"pattern": "def foo", "path": str(tmp_path)})
    assert result.success
    assert "def foo" in result.output
    assert "def bar" not in result.output


@pytest.mark.asyncio
async def test_grep_tool_fallback_non_python(tmp_path: Path):
    """GrepTool fallback should search non-Python files (e.g., .txt) via rglob("*")."""
    (tmp_path / "notes.txt").write_text("TODO: fix the login bug\n")
    tool = GrepTool()
    result = await tool.execute({"pattern": "login bug", "path": str(tmp_path)})
    assert result.success
    assert "login bug" in result.output
