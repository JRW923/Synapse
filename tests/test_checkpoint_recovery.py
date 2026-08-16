"""ReAct × Checkpoint integration — thrashing auto-recovery rolls the file back."""

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

from synapse.protocols.llm import LLMResponse, Message
from synapse.protocols.retriever import Context
from synapse.protocols.tool import RiskLevel, ToolResult, ToolCallMetadata


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r.stdout


class _WriteTool:
    """Deterministic 'edit' tool: touches the same file every call."""
    name = "edit"
    risk_level = RiskLevel.WRITE_LOCAL

    def __init__(self, target: Path):
        self.target = target

    def get_schema(self):
        return {"name": "edit", "description": "edit", "input_schema": {}}

    async def execute(self, params, sandbox=None):
        self.target.write_text("attempt\n", encoding="utf-8")
        return ToolResult(
            success=True, output="ok",
            metadata=ToolCallMetadata(tool_name="edit", files_touched=[str(self.target)]),
        )


def test_thrashing_auto_recovery_restores_file(tmp_path: Path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    original = "print('v1')\n"
    (tmp_path / "app.py").write_text(original, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")

    tool = _WriteTool(tmp_path / "app.py")

    class _Registry:
        def get(self, name):
            return tool if name == "edit" else None

        def get_schemas(self):
            return [{"name": "edit", "description": "edit", "input_schema": {}}]

    tools = _Registry()
    llm = AsyncMock()
    llm.chat.side_effect = [
        LLMResponse(content="", tool_calls=[
            {"id": f"t{i}", "name": "edit", "input": {}} for i in range(3)
        ], stop_reason="tool_use", usage={"input": 10, "output": 5}),
        LLMResponse(content="done", tool_calls=[], stop_reason="end_turn",
                    usage={"input": 5, "output": 2}),
    ]
    llm.stream = None  # force chat() path

    from synapse.modules.planning.react import ReActPlanner
    from synapse.core.session import Session
    from synapse.core.events import EventBus

    planner = ReActPlanner(
        max_iterations=5, thrashing_threshold=3, max_thrashing_events=5,
        checkpoint_enabled=True, auth=None,
    )
    # _WriteTool needs no auth context; wire cwd to the tmp repo.
    import synapse.modules.planning.react as react_mod
    original_wd = react_mod._working_directory
    react_mod._working_directory = lambda auth: str(tmp_path)
    try:
        result = asyncio.run(planner.execute(
            task="fix app.py", context=Context(), tools=tools, llm=llm,
            sandbox=None, session=Session(), event_bus=EventBus(),
        ))
    finally:
        react_mod._working_directory = original_wd

    # The 3rd edit trips the threshold → file was auto-rolled back to v1.
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == original
    assert result.metrics.thrashing_events >= 1
