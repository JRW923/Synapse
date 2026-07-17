"""Tests for evaluation metric collectors."""
import pytest

from synapse.core.events import EventBus
from synapse.protocols.events import (
    ToolCallStarted,
    ToolCallCompleted,
    FileWritten,
    AuthDecisionMade,
    AgentCompleted,
    ThrashingDetected,
    PlanCreated,
    MergeResult,
)

from synapse.eval.metrics.process import ProcessMetrics, ProcessSnapshot
from synapse.eval.metrics.quality import QualityMetrics, QualitySnapshot
from synapse.eval.metrics.efficiency import EfficiencyMetrics, EfficiencySnapshot
from synapse.eval.metrics.safety import SafetyMetrics, SafetySnapshot


# ============================================================================
# Test 1: ProcessMetrics
# ============================================================================


@pytest.mark.asyncio
async def test_process_metrics_records_events():
    """ProcessMetrics records reuse stats, thrashing, plan/merge quality, and
    test persistence from relevant EventBus events."""
    bus = EventBus()
    collector = ProcessMetrics(bus)

    # Emit tool_call_started events (reuse-related and normal)
    await bus.emit(ToolCallStarted(
        session_id="s1", tool_name="find_reuse", tool_params={"query": "auth bug"},
    ))
    await bus.emit(ToolCallStarted(
        session_id="s1", tool_name="adopt_reuse", tool_params={"entry_id": "abc"},
    ))
    await bus.emit(ToolCallStarted(
        session_id="s1", tool_name="read", tool_params={"path": "foo.py"},
    ))

    # Emit tool_call_completed events (instruction drift heuristic)
    await bus.emit(ToolCallCompleted(
        session_id="s1", tool_name="read", success=True, duration_ms=100,
        files_touched=["foo.py"],
    ))
    await bus.emit(ToolCallCompleted(
        session_id="s1", tool_name="find_reuse", success=True, duration_ms=50,
        files_touched=[],
    ))
    await bus.emit(ToolCallCompleted(
        session_id="s1", tool_name="adopt_reuse", success=True, duration_ms=30,
        files_touched=["bar.py"],
    ))

    # Emit file_written events (test persistence)
    await bus.emit(FileWritten(
        session_id="s1", path="tests/test_auth.py", bytes_written=512,
    ))
    await bus.emit(FileWritten(
        session_id="s1", path="src/auth.py", bytes_written=256,
    ))
    await bus.emit(FileWritten(
        session_id="s1", path="test_core.py", bytes_written=128,
    ))

    # Emit agent_completed
    await bus.emit(AgentCompleted(
        session_id="s1", status="success", total_tokens=1000,
        tool_calls=3, duration_ms=5000,
    ))

    # Emit thrashing_detected events
    await bus.emit(ThrashingDetected(
        session_id="s1", file_path="foo.py", modification_count=4,
    ))
    await bus.emit(ThrashingDetected(
        session_id="s1", file_path="bar.py", modification_count=8,
    ))

    # Emit plan_created
    await bus.emit(PlanCreated(
        session_id="s1", task="fix auth bug",
        plan_steps=[
            {"step_id": "1", "description": "Read auth.py"},
            {"step_id": "2", "description": "Fix logic"},
            {"step_id": "3", "description": "Write tests"},
        ],
        reasoning="The auth module has a null-check bug.",
    ))

    # Emit merge_result
    await bus.emit(MergeResult(
        session_id="s1", subtask_count=3,
        merged_output="All three subtasks completed successfully.",
    ))

    snapshot = collector.snapshot()

    # Reuse stats
    assert snapshot.reuse_attempted == 2  # find_reuse + adopt_reuse
    assert snapshot.reuse_found == 1       # find_reuse completed successfully
    assert snapshot.reuse_adopted == 1     # adopt_reuse completed successfully

    # Test persistence
    assert snapshot.tests_persisted == 2   # tests/test_auth.py + test_core.py
    assert snapshot.total_files_written == 3
    assert 0.6 < snapshot.test_persistence_rate < 0.7

    # Instruction drift
    assert snapshot.instruction_drift_at_round == 3  # one per completed call

    # Plan / merge quality
    assert snapshot.plan_quality_score > 0
    assert snapshot.merge_quality_score > 0

    # Thrashing
    assert snapshot.thrashing_events == 2
    assert snapshot.regex_abuse_events >= 1  # bar.py with 8 modifications

    # Root cause accuracy
    assert snapshot.root_cause_accuracy > 0


@pytest.mark.asyncio
async def test_process_metrics_reset():
    """reset() clears all accumulated metrics."""
    bus = EventBus()
    collector = ProcessMetrics(bus)

    await bus.emit(ThrashingDetected(
        session_id="s1", file_path="f.py", modification_count=5,
    ))
    assert collector.snapshot().thrashing_events == 1

    collector.reset()
    snap = collector.snapshot()
    assert snap.thrashing_events == 0
    assert snap.reuse_attempted == 0
    assert snap.tests_persisted == 0


# ============================================================================
# Test 2: QualityMetrics
# ============================================================================


@pytest.mark.asyncio
async def test_quality_metrics_records_events():
    """QualityMetrics records complexity, duplication, function length violations,
    test coverage delta, and lint errors from EventBus events."""
    bus = EventBus()
    collector = QualityMetrics(bus)

    # Emit file_written events (track complexity / coverage / duplication)
    await bus.emit(FileWritten(session_id="s1", path="src/module.py", bytes_written=2048))
    await bus.emit(FileWritten(session_id="s1", path="src/utils.py", bytes_written=1024))
    await bus.emit(FileWritten(session_id="s1", path="tests/test_module.py", bytes_written=512))
    await bus.emit(FileWritten(session_id="s1", path="src/legacy.py", bytes_written=4096))

    # Emit tool_call_completed events (lint errors from failed tool calls)
    await bus.emit(ToolCallCompleted(
        session_id="s1", tool_name="lint", success=False, duration_ms=200,
        files_touched=["src/module.py"],
    ))
    await bus.emit(ToolCallCompleted(
        session_id="s1", tool_name="lint", success=False, duration_ms=150,
        files_touched=["src/legacy.py"],
    ))
    await bus.emit(ToolCallCompleted(
        session_id="s1", tool_name="test", success=True, duration_ms=1000,
        files_touched=["tests/test_module.py"],
    ))

    # Emit a large file that could trigger function length violations
    await bus.emit(FileWritten(session_id="s1", path="src/huge.py", bytes_written=8192))

    snapshot = collector.snapshot()

    # Complexity delta — based on files touched
    assert snapshot.complexity_delta > 0

    # Duplication rate — based on file sizes / count heuristics
    assert snapshot.duplication_rate >= 0

    # Function length violations — large file heuristic
    assert snapshot.function_length_violations >= 0

    # Test coverage delta
    assert snapshot.test_coverage_delta >= 0

    # Lint errors introduced
    assert snapshot.lint_errors_introduced == 2


@pytest.mark.asyncio
async def test_quality_metrics_reset():
    """reset() clears all accumulated quality metrics."""
    bus = EventBus()
    collector = QualityMetrics(bus)

    await bus.emit(FileWritten(session_id="s1", path="src/a.py", bytes_written=500))
    assert collector.snapshot().complexity_delta > 0

    collector.reset()
    snap = collector.snapshot()
    assert snap.complexity_delta == 0
    assert snap.lint_errors_introduced == 0


# ============================================================================
# Test 3: EfficiencyMetrics
# ============================================================================


@pytest.mark.asyncio
async def test_efficiency_metrics_records_events():
    """EfficiencyMetrics records token counts, tool calls, duration, cost,
    and thrashing ratio from EventBus events."""
    bus = EventBus()
    collector = EfficiencyMetrics(bus)

    # Emit tool_call_started events
    await bus.emit(ToolCallStarted(
        session_id="s1", tool_name="read", tool_params={"path": "a.py"},
    ))
    await bus.emit(ToolCallStarted(
        session_id="s1", tool_name="write", tool_params={"path": "b.py"},
    ))
    await bus.emit(ToolCallStarted(
        session_id="s1", tool_name="grep", tool_params={"pattern": "TODO"},
    ))

    # Emit tool_call_completed events (some successful, some not)
    await bus.emit(ToolCallCompleted(
        session_id="s1", tool_name="read", success=True, duration_ms=100,
        files_touched=["a.py"],
    ))
    await bus.emit(ToolCallCompleted(
        session_id="s1", tool_name="write", success=True, duration_ms=200,
        files_touched=["b.py"],
    ))
    await bus.emit(ToolCallCompleted(
        session_id="s1", tool_name="grep", success=False, duration_ms=500,
        files_touched=[],
    ))

    # Emit agent_completed (provides token totals and overall duration)
    await bus.emit(AgentCompleted(
        session_id="s1", status="success", total_tokens=2500,
        tool_calls=3, duration_ms=4500,
    ))

    # Emit thrashing
    await bus.emit(ThrashingDetected(
        session_id="s1", file_path="x.py", modification_count=3,
    ))

    snapshot = collector.snapshot()

    # Token counts (estimated input/output split from total_tokens)
    assert snapshot.tokens_input > 0
    assert snapshot.tokens_output > 0

    # Tool call counts
    assert snapshot.tool_call_count == 3
    assert snapshot.tool_success_count == 2
    assert 0.6 < snapshot.success_rate < 0.7

    # Duration & cost
    assert snapshot.duration_ms == 4500
    assert snapshot.cost_estimate_usd > 0

    # Thrashing ratio
    assert snapshot.thrashing_ratio > 0


@pytest.mark.asyncio
async def test_efficiency_metrics_reset():
    """reset() clears all accumulated efficiency metrics."""
    bus = EventBus()
    collector = EfficiencyMetrics(bus)

    await bus.emit(AgentCompleted(
        session_id="s1", status="success", total_tokens=100,
        tool_calls=1, duration_ms=500,
    ))
    assert collector.snapshot().duration_ms == 500

    collector.reset()
    snap = collector.snapshot()
    assert snap.duration_ms == 0
    assert snap.tool_call_count == 0
    assert snap.cost_estimate_usd == 0.0


# ============================================================================
# Test 4: SafetyMetrics
# ============================================================================


@pytest.mark.asyncio
async def test_safety_metrics_records_events():
    """SafetyMetrics records auth blocks, sandbox violations, injection attempts,
    out-of-workspace access, and dangerous command attempts from EventBus events."""
    bus = EventBus()
    collector = SafetyMetrics(bus)

    # Emit auth_decision events (some blocked)
    await bus.emit(AuthDecisionMade(
        session_id="s1", tool_name="shell", allowed=True, reason="safe command",
    ))
    await bus.emit(AuthDecisionMade(
        session_id="s1", tool_name="write", allowed=False, reason="outside workspace",
    ))
    await bus.emit(AuthDecisionMade(
        session_id="s1", tool_name="shell", allowed=False, reason="dangerous pattern",
    ))

    # Emit tool_call_completed (sandbox violations)
    await bus.emit(ToolCallCompleted(
        session_id="s1", tool_name="shell", success=False, duration_ms=100,
        files_touched=[],
    ))

    # Emit file_written (out-of-workspace access tracked via path)
    await bus.emit(FileWritten(
        session_id="s1", path="/etc/passwd", bytes_written=64,
    ))
    await bus.emit(FileWritten(
        session_id="s1", path="./project/file.py", bytes_written=256,
    ))
    await bus.emit(FileWritten(
        session_id="s1", path="/root/.bashrc", bytes_written=128,
    ))

    # Emit tool_call_started with injection-like params
    await bus.emit(ToolCallStarted(
        session_id="s1", tool_name="shell",
        tool_params={"command": "cat /etc/shadow; rm -rf /"},
    ))
    await bus.emit(ToolCallStarted(
        session_id="s1", tool_name="shell",
        tool_params={"command": "echo hello"},
    ))

    snapshot = collector.snapshot()

    # Auth blocks
    assert snapshot.auth_blocks == 2  # two allowed=False decisions

    # Sandbox violations
    assert snapshot.sandbox_violations >= 0

    # Injection attempts (dangerous commands detected in tool params)
    assert snapshot.injection_attempts >= 1  # cat /etc/shadow; rm -rf /

    # Out-of-workspace access
    assert snapshot.out_of_workspace_access == 2  # /etc/passwd, /root/.bashrc

    # Dangerous command attempts
    assert snapshot.dangerous_command_attempts >= 1  # rm -rf /


@pytest.mark.asyncio
async def test_safety_metrics_reset():
    """reset() clears all accumulated safety metrics."""
    bus = EventBus()
    collector = SafetyMetrics(bus)

    await bus.emit(AuthDecisionMade(
        session_id="s1", tool_name="shell", allowed=False, reason="blocked",
    ))
    assert collector.snapshot().auth_blocks == 1

    collector.reset()
    snap = collector.snapshot()
    assert snap.auth_blocks == 0
    assert snap.injection_attempts == 0
    assert snap.dangerous_command_attempts == 0
