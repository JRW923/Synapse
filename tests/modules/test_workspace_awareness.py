"""Tests: workspace awareness — (1) system prompt tells the LLM its working
directory, (2) WriteTool resolves relative paths against the workspace root.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from synapse.modules.planning.react import ReActPlanner, _working_directory
from synapse.modules.tools.file_write import WriteTool


# --- _working_directory helper -------------------------------------------------

def test_working_directory_prefers_auth_workspace_root():
    assert _working_directory(SimpleNamespace(workspace_root="/ws")) == "/ws"


def test_working_directory_falls_back_to_cwd():
    assert _working_directory(None) == str(Path.cwd())


# --- system prompt injection ---------------------------------------------------

def _planner_with(ws_root):
    auth = SimpleNamespace(workspace_root=ws_root)
    return ReActPlanner(auth=auth)


def test_system_prompt_includes_working_directory():
    planner = _planner_with("/my/work/dir")
    ctx = SimpleNamespace(system=[], core=[], reference=[])
    prompt = planner._build_system_prompt(ctx, task="do something")
    assert "Your working directory is /my/work/dir" in prompt
    assert "run the narrowest relevant tests" in prompt
    assert "do not claim success from intent alone" in prompt


# --- WriteTool relative-path resolution --------------------------------------

@pytest.mark.asyncio
async def test_relative_path_resolves_to_workspace_root(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = WriteTool(workspace_root=str(ws))
    res = await tool.execute({"path": "sub/report.md", "content": "hello"})
    assert res.success
    target = ws / "sub" / "report.md"
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "hello"


@pytest.mark.asyncio
async def test_absolute_path_is_not_rebased(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    other = tmp_path / "other" / "x.md"
    tool = WriteTool(workspace_root=str(ws))
    res = await tool.execute({"path": str(other), "content": "y"})
    assert res.success
    assert other.exists()
    assert not (ws / "other").exists()


@pytest.mark.asyncio
async def test_resolve_path_helper_relative():
    tool = WriteTool(workspace_root="/ws")
    # Compare against the tool's own resolved root (portable across OSes).
    assert tool._resolve_path("a/b.md") == tool._workspace_root / "a" / "b.md"


@pytest.mark.asyncio
async def test_resolve_path_helper_absolute_unaffected():
    tool = WriteTool(workspace_root="/ws")
    # An absolute path on the same drive must not be rebased into the workspace.
    abs_path = tool._workspace_root.parent / "elsewhere" / "x.md"
    assert tool._resolve_path(str(abs_path)) == abs_path


@pytest.mark.asyncio
async def test_no_workspace_root_keeps_relative_as_is():
    tool = WriteTool()
    assert tool._resolve_path("flat.md") == Path("flat.md")
