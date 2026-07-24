"""End-to-end + unit tests for the security red-team framework (TODO F).

Drives the real Agent (real tools / real ActionAuthorizer / real ProcessSandbox)
with a scripted attacker LLM, and asserts the defender neutralizes the attacks
that it should, and honestly surfaces the ones it doesn't.

No real LLM or network — every attack carries its own tool-call script.
"""

import pytest
from synapse.core.container import Container
from synapse.core.agent import Agent
from synapse.core.session import Session
from synapse.core.events import EventBus
from synapse.protocols.llm import LLMProvider
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
from synapse.modules.security.auth import ActionAuthorizer
from synapse.modules.planning.react import ReActPlanner
from synapse.eval.metrics.safety import SafetyMetrics
from synapse.eval.redteam.attacks import (
    AttackCase, AttackCategory, AttackStep, DefenseOutcome, seed_attacks,
)
from synapse.eval.redteam.runner import AttackLLM, RedTeamRunner


class _NullRetriever:
    """Avoid filesystem scans; returns an empty context."""

    async def retrieve(self, task, project_root, tools, memory, budget):
        return Context()


def build_redteam_container(attack: AttackCase, tmp_path, bus: EventBus) -> Container:
    c = Container()
    c.register(EventBus, bus)
    c.register(LLMProvider, AttackLLM(attack.steps))

    registry = DefaultToolRegistry()
    for t in (ReadTool(), WriteTool(), EditTool(), GlobTool(), GrepTool(), ShellTool(), GitTool()):
        registry.register(t)
    c.register(ToolRegistry, registry)

    c.register(MemoryStore, SessionMemory())
    c.register(ContextRetriever, _NullRetriever())
    c.register(Sandbox, ProcessSandbox())

    auth = ActionAuthorizer(workspace_root=str(tmp_path), allow_external=False)
    c.register(Planner, ReActPlanner(max_iterations=10, auth=auth))
    return c


@pytest.mark.asyncio
async def test_redteam_seed_e2e(tmp_path):
    bus = EventBus()
    # One SafetyMetrics subscriber for the whole run (proves the auth_decision
    # / file_written emits added in react.py actually feed the scorer).
    safety = SafetyMetrics(bus, workspace_root=str(tmp_path))

    def run_attack(attack: AttackCase):
        c = build_redteam_container(attack, tmp_path, bus)
        return Agent(c).run(attack.description, Session())

    attacks = seed_attacks()
    runner = RedTeamRunner(bus)
    report = await runner.run(
        attacks, run_attack, name="seed", safety_snapshot=safety.snapshot()
    )

    hardened = [a for a in attacks if not a.known_gap]
    gaps = [a for a in attacks if a.known_gap]

    # Every hardened attack is neutralized by a hard-deny.
    for a in hardened:
        res = next(r for r in report.results if r.id == a.id)
        assert res.actual == DefenseOutcome.BLOCKED, f"{a.id} should be blocked"
        assert res.passed, f"{a.id} should pass (defender neutralized it)"

    # Known gaps are honestly surfaced as findings (defender allowed them).
    for a in gaps:
        res = next(r for r in report.results if r.id == a.id)
        assert res.actual == DefenseOutcome.ALLOWED, f"{a.id} is a known gap (allowed)"
        assert not res.passed
    assert len(report.findings) == len(gaps)

    # The auth_decision emit fix makes auth_blocks observable.
    assert safety.snapshot().auth_blocks > 0

    # Report is coherent and printable.
    assert 0.0 <= report.pass_rate <= 1.0
    assert report.passed == len(hardened)
    assert report.failed == len(gaps)
    assert "Red-team report" in report.to_text()


@pytest.mark.asyncio
async def test_attack_llm_replays_steps():
    steps = [
        AttackStep(tool="shell", params={"command": "rm -rf /"}),
        AttackStep(tool=None, content="done"),
    ]
    llm = AttackLLM(steps)
    first = await llm.chat([])
    assert first.tool_calls and first.tool_calls[0]["name"] == "shell"
    second = await llm.chat([])
    assert second.tool_calls == [] and second.content == "done"
    # Exhausted → keeps finishing.
    third = await llm.chat([])
    assert third.tool_calls == []


@pytest.mark.asyncio
async def test_runner_scoring_aggregation():
    """Runner scores purely from auth_decision events (no real agent)."""
    bus = EventBus()
    runner = RedTeamRunner(bus)

    # blocked attack → caller emits a denied auth_decision
    async def run_attack(attack: AttackCase):
        if attack.id == "blk":
            await bus.emit(_auth_denied())

    attacks = [
        AttackCase(id="blk", category=AttackCategory.SANDBOX_ESCAPE,
                   name="x", description="x", steps=[],
                   expected_defense=DefenseOutcome.BLOCKED),
        AttackCase(id="ok", category=AttackCategory.PROMPT_INJECTION_INDIRECT,
                   name="y", description="y", steps=[],
                   expected_defense=DefenseOutcome.BLOCKED, known_gap=True),
    ]
    report = await runner.run(attacks, run_attack, name="unit")
    assert report.passed == 1
    assert report.failed == 1
    assert report.findings[0].id == "ok"
    assert report.pass_rate == 0.5


# Duck-typed stand-in for a denied auth_decision event.
def _auth_denied():
    class _Evt:
        event_type = "auth_decision"
        allowed = False
        reason = "test deny"
    return _Evt()
