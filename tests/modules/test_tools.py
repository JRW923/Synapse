"""Tests for basic tools."""
import asyncio
import os
import tempfile
from pathlib import Path
import pytest
from synapse.modules.tools.file_read import ReadTool
from synapse.modules.tools.file_write import WriteTool
from synapse.modules.tools.file_edit import EditTool
from synapse.modules.tools.file_glob import GlobTool
from synapse.modules.tools.search_grep import GrepTool
from synapse.modules.tools.shell import ShellTool
from synapse.modules.tools.git_ import GitTool
from synapse.modules.tools.registry import DefaultToolRegistry


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


@pytest.mark.asyncio
async def test_shell_tool_echo():
    tool = ShellTool()
    result = await tool.execute({"command": "echo hello"})
    assert result.success
    assert "hello" in result.output


@pytest.mark.asyncio
async def test_shell_tool_timeout():
    tool = ShellTool()
    result = await tool.execute({"command": "sleep 10"}, timeout=1)
    assert not result.success
    assert result.error is not None


@pytest.mark.asyncio
async def test_git_tool_log(tmp_path: Path):
    import subprocess
    # Initialize a git repo
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    (tmp_path / "file.txt").write_text("content")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

    tool = GitTool()
    result = await tool.execute({"command": "log", "cwd": str(tmp_path)})
    assert result.success
    assert "init" in result.output


@pytest.mark.asyncio
async def test_git_tool_diff(tmp_path: Path):
    import subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    (tmp_path / "file.txt").write_text("before")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
    (tmp_path / "file.txt").write_text("after")

    tool = GitTool()
    result = await tool.execute({"command": "diff", "cwd": str(tmp_path)})
    assert result.success
    assert "before" in result.output or "after" in result.output


def test_tool_registry():
    registry = DefaultToolRegistry()
    read = ReadTool()
    registry.register(read)

    assert registry.get("read") is read
    assert len(registry.list_all()) == 1
    schemas = registry.get_schemas()
    assert len(schemas) == 1
    assert schemas[0]["name"] == "read"


def test_registry_list_by_category():
    registry = DefaultToolRegistry()
    read = ReadTool()
    write = WriteTool()
    registry.register(read)
    registry.register(write)

    file_tools = registry.list_by_category("file")
    assert len(file_tools) == 2
