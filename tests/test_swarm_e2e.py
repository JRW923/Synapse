"""End-to-end test of swarm mode through the full Agent pipeline.

Builds a real Container (real tools, real sandbox, real Session.fork) but with
a *content-routed* scripted LLM instead of a live provider. Because the two
parallel coders share one LLM, responses are chosen by inspecting the prompt
(decompose / merge / reviewer / coder) rather than by call order — which would
be unstable under asyncio.gather.

Exercises: CLI-style SwarmPlanner selection -> decompose -> parallel coder
workers writing distinct files -> merge -> reviewer approve, and the reject ->
re-run-weakest-coder -> re-merge -> approve verification loop.
"""

import pytest
from synapse.core.container import Container
from synapse.core.agent import Agent
from synapse.core.session import Session
from synapse.core.events import EventBus
from synapse.protocols.llm import LLMProvider, LLMResponse
from synapse.protocols.planner import Planner
from synapse.protocols.tool import ToolRegistry
from synapse.protocols.memory import MemoryStore
from synapse.protocols.retriever import Context, ContextRetriever
from synapse.protocols.sandbox import Sandbox
from synapse.modules.tools.registry import DefaultToolRegistry
from synapse.modules.tools.file_read import ReadTool
from synapse.modules.tools.file_write import WriteTool
from synapse.modules.tools.file_edit import EditTool
from synapse.modules.tools.file_glob import GlobTool
from synapse.modules.tools.search_grep import GrepTool
from synapse.modules.tools.shell import ShellTool
from synapse.modules.tools.git_ import GitTool
from synapse.modules.memory.session import SessionMemory
from synapse.modules.security.sandbox import ProcessSandbox
from synapse.modules.planning.react import ReActPlanner
from synapse.modules.planning.swarm import SwarmPlanner
from synapse.protocols.events import (
    WorkerSpawned, WorkerCompleted, ReviewSubmitted, VoteCast, SwarmVerified,
)


class _NullRetriever:
    """Avoid filesystem scans; returns an empty context so the test focuses
    on the swarm pipeline rather than retrieval."""

    async def retrieve(self, task, project_root, tools, memory, budget):
        return Context()


class _ScriptedSwarmLLM:
    """Routes each LLM call by prompt content.

    - decompose prompt -> JSON with 2 non-overlapping file scopes
    - merge prompt     -> merged text
    - reviewer prompt   -> pops from ``reviewer_script`` (approve / reject)
    - coder prompt      -> writes its file on the first turn, finishes after
    """

    def __init__(self, tmp_path, reviewer_script):
        self._tmp = tmp_path
        self._reviewer_script = list(reviewer_script)
        self.reviewer_calls = 0

    async def chat(self, messages, tools=None):
        system = messages[0].content if messages else ""
        first_user = next((m.content for m in messages if m.role == "user"), "")

        # 1) Swarm decomposition into disjoint file scopes.
        if "task decomposition expert" in system and "parallel subtasks" in system:
            return LLMResponse(
                content=(
                    '[{"id":"1","description":"实现 a.py 模块","file_scope":"a.py"},'
                    '{"id":"2","description":"实现 b.py 模块","file_scope":"b.py"}]'
                ),
                tool_calls=[], stop_reason="end_turn", usage={"input": 1, "output": 1},
            )

        # 2) Result merging.
        if "result-merging expert" in system:
            return LLMResponse(
                content="合并结果：a.py 与 b.py 均已实现，功能完整。",
                tool_calls=[], stop_reason="end_turn", usage={"input": 1, "output": 1},
            )

        # 3) Reviewer verdict — driven by the test's script.
        if "代码审查" in system or "审查" in system:
            self.reviewer_calls += 1
            verdict = self._reviewer_script.pop(0) if self._reviewer_script else "通过，满足任务。"
            return LLMResponse(
                content=verdict, tool_calls=[], stop_reason="end_turn",
                usage={"input": 1, "output": 1},
            )

        # 4) Coder: write once, then finish. A coder reuses the same first
        # user message on retry, so decide by whether a write already happened.
        already_wrote = any(
            m.role == "assistant" and m.tool_calls
            and any(tc.get("name") == "write" for tc in m.tool_calls)
            for m in messages
        )
        fname = "a.py" if "a.py" in first_user else "b.py"
        path = self._tmp / fname
        if not already_wrote:
            return LLMResponse(
                content="",
                tool_calls=[{"id": "w1", "name": "write",
                             "input": {"path": str(path),
                                       "content": f"# {fname}\nprint('{fname}')\n"}}],
                stop_reason="tool_use", usage={"input": 1, "output": 1},
            )
        return LLMResponse(
            content=f"已完成 {fname}", tool_calls=[], stop_reason="end_turn",
            usage={"input": 1, "output": 1},
        )


def _build_swarm_container(tmp_path, reviewer_script):
    c = Container()
    c.register(EventBus, EventBus())
    c.register(LLMProvider, _ScriptedSwarmLLM(tmp_path, reviewer_script))

    registry = DefaultToolRegistry()
    for t in (ReadTool(), WriteTool(), EditTool(), GlobTool(), GrepTool(), ShellTool(), GitTool()):
        registry.register(t)
    c.register(ToolRegistry, registry)

    c.register(MemoryStore, SessionMemory())
    c.register(ContextRetriever, _NullRetriever())
    c.register(Sandbox, ProcessSandbox())

    react = ReActPlanner(max_iterations=10)
    c.register(Planner, SwarmPlanner(react_planner=react))
    return c


@pytest.mark.asyncio
async def test_swarm_e2e_approve(tmp_path):
    c = _build_swarm_container(tmp_path, reviewer_script=["通过，合并结果满足任务。"])
    bus = c.resolve(EventBus)
    captured = []

    async def _cap(e, et):
        captured.append((et, e))

    for et in (WorkerSpawned, WorkerCompleted, ReviewSubmitted, VoteCast, SwarmVerified):
        bus.subscribe(et.event_type, lambda e, et=et: _cap(e, et))

    agent = Agent(c)
    session = Session()
    result = await agent.run("实现功能 X", session)

    # Full pipeline produced a successful swarm result.
    assert result.role == "swarm"
    assert result.status.value == "success"
    assert "a.py" in result.output and "b.py" in result.output

    # Both coders actually wrote their own files (parallel, isolated work).
    assert (tmp_path / "a.py").exists()
    assert (tmp_path / "b.py").exists()

    # Swarm event lifecycle is complete and correctly attributed.
    types = [t for t, _ in captured]
    assert types.count(WorkerSpawned) == 3      # 2 coders + 1 reviewer
    assert types.count(WorkerCompleted) == 3
    assert types.count(ReviewSubmitted) == 1
    assert types.count(VoteCast) == 1
    assert [e for t, e in captured if t is SwarmVerified][0].status == "success"


@pytest.mark.asyncio
async def test_swarm_e2e_reject_then_approve(tmp_path):
    # Reviewer rejects first, then approves the re-merged result.
    c = _build_swarm_container(
        tmp_path, reviewer_script=["不通过，缺少错误处理。", "通过，已补齐。"]
    )
    bus = c.resolve(EventBus)
    captured = []

    async def _cap(e, et):
        captured.append((et, e))

    for et in (WorkerSpawned, WorkerCompleted, ReviewSubmitted, VoteCast, SwarmVerified):
        bus.subscribe(et.event_type, lambda e, et=et: _cap(e, et))

    agent = Agent(c)
    session = Session()
    result = await agent.run("实现功能 X", session)

    # Verification loop recovered: weakest coder re-ran and final verdict passed.
    assert result.status.value == "success"

    reviews = [e.verdict for t, e in captured if t is ReviewSubmitted]
    assert reviews == ["reject", "approve"]

    # Exactly one reject then one approve vote, and swarm ultimately verified.
    votes = [e.decision for t, e in captured if t is VoteCast]
    assert votes == ["reject", "approve"]
    assert [e for t, e in captured if t is SwarmVerified][0].status == "success"
