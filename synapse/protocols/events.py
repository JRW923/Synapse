"""Event types for the Synapse EventBus — pure data, no behavior."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import uuid


class EventType(str, Enum):
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    FILE_WRITTEN = "file_written"
    AUTH_DECISION = "auth_decision"
    AGENT_ERROR = "agent_error"
    AGENT_COMPLETED = "agent_completed"
    AGENT_PROGRESS = "agent_progress"
    THRASHING_DETECTED = "thrashing_detected"
    PLAN_CREATED = "plan_created"
    TASK_DECOMPOSED = "task_decomposed"
    MERGE_RESULT = "merge_result"
    CONTEXT_BLOCK_CITED = "context_block_cited"
    LLM_TOKEN = "llm_token"
    PROCESS_QUALITY_SCORED = "process_quality_scored"
    WORKER_SPAWNED = "worker_spawned"
    WORKER_COMPLETED = "worker_completed"
    REVIEW_SUBMITTED = "review_submitted"
    VOTE_CAST = "vote_cast"
    SWARM_VERIFIED = "swarm_verified"
    TASK_STATUS_CHANGED = "task_status_changed"
    TASK_CLAIMED = "task_claimed"
    TASK_RELEASED = "task_released"
    BACKGROUND_RESULT = "background_result"
    AGENT_MESSAGE = "agent_message"


@dataclass(kw_only=True)
class BaseEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=datetime.now)
    session_id: str
    # Correlate every event with one logical run and its causal predecessor.
    run_id: str = ""
    trace_id: str = ""
    parent_event_id: str = ""
    # Swarm/Team attribution — empty for non-swarm events.
    agent_id: str = ""
    role: str = ""


@dataclass(kw_only=True)
class ToolCallStarted(BaseEvent):
    event_type: str = EventType.TOOL_CALL_STARTED
    tool_name: str
    tool_params: dict


@dataclass(kw_only=True)
class ToolCallCompleted(BaseEvent):
    event_type: str = EventType.TOOL_CALL_COMPLETED
    tool_name: str
    success: bool
    duration_ms: int
    files_touched: list[str] = field(default_factory=list)
    sandbox_violation: bool = False


@dataclass(kw_only=True)
class FileWritten(BaseEvent):
    event_type: str = EventType.FILE_WRITTEN
    path: str
    bytes_written: int


@dataclass(kw_only=True)
class AuthDecisionMade(BaseEvent):
    event_type: str = EventType.AUTH_DECISION
    tool_name: str
    allowed: bool
    reason: str


@dataclass(kw_only=True)
class AgentError(BaseEvent):
    event_type: str = EventType.AGENT_ERROR
    error_type: str
    message: str


@dataclass(kw_only=True)
class AgentCompleted(BaseEvent):
    event_type: str = EventType.AGENT_COMPLETED
    status: str  # "success" | "partial" | "failed"
    total_tokens: int
    tool_calls: int
    duration_ms: int
    # Exact split when the planner/provider exposes it. Older producers can
    # keep sending only total_tokens; metric collectors retain a fallback.
    tokens_input: int | None = None
    tokens_output: int | None = None


@dataclass(kw_only=True)
class AgentProgress(BaseEvent):
    """Emitted at key steps in the agent loop for live progress display."""
    event_type: str = EventType.AGENT_PROGRESS
    phase: str  # "thinking" | "calling_llm" | "executing_tool" | "done"
    message: str = ""


@dataclass(kw_only=True)
class ThrashingDetected(BaseEvent):
    event_type: str = EventType.THRASHING_DETECTED
    file_path: str
    modification_count: int


@dataclass(kw_only=True)
class PlanCreated(BaseEvent):
    event_type: str = EventType.PLAN_CREATED
    task: str
    plan_steps: list[dict] = field(default_factory=list)
    reasoning: str = ""


@dataclass(kw_only=True)
class TaskDecomposed(BaseEvent):
    event_type: str = EventType.TASK_DECOMPOSED
    subtask_ids: list[str] = field(default_factory=list)
    subtask_count: int = 0


@dataclass(kw_only=True)
class MergeResult(BaseEvent):
    event_type: str = EventType.MERGE_RESULT
    subtask_count: int = 0
    merged_output: str = ""


@dataclass(kw_only=True)
class ContextBlockCited(BaseEvent):
    """Phase 4 — emitted when an LLM response appears to cite a context block."""
    event_type: str = EventType.CONTEXT_BLOCK_CITED
    block_id: str
    block_source: str
    response_snippet: str = ""


@dataclass(kw_only=True)
class LLMToken(BaseEvent):
    """Streaming LLM text chunk — emitted per token for live CLI display."""
    event_type: str = EventType.LLM_TOKEN
    text: str
    # When the provider streams usage, this carries the cumulative
    # {"input": N, "output": M} for the current request so the CLI can tick
    # the token counter up smoothly instead of jumping once per response.
    # None when the provider does not expose per-chunk usage.
    usage: dict | None = None


@dataclass(kw_only=True)
class ProcessQualityScored(BaseEvent):
    """Emitted after a task completes — the process-quality verification result.

    Carries the composite score plus the signals that produced it, and a
    natural-language ``hint`` intended to be fed back into the next task.
    """
    event_type: str = EventType.PROCESS_QUALITY_SCORED
    task: str
    score: float                      # 0..1 composite process-quality score
    reuse_ratio: float                # fraction of writes preceded by a lookup
    write_without_lookup: int         # writes with no preceding lookup
    thrashing_events: int
    success: bool
    tool_calls: int
    hint: str                         # feedback for the next run


# ---- Swarm / Team -------------------------------------------------


@dataclass(kw_only=True)
class WorkerSpawned(BaseEvent):
    """A swarm worker (role) was spawned for a slice of the task."""
    event_type: str = EventType.WORKER_SPAWNED
    agent_id: str
    role: str
    task: str


@dataclass(kw_only=True)
class WorkerCompleted(BaseEvent):
    """A swarm worker finished its slice."""
    event_type: str = EventType.WORKER_COMPLETED
    agent_id: str
    role: str
    status: str
    output_snippet: str = ""


@dataclass(kw_only=True)
class ReviewSubmitted(BaseEvent):
    """A reviewer/verifier submitted a review of another worker's output."""
    event_type: str = EventType.REVIEW_SUBMITTED
    agent_id: str
    reviewer_role: str
    target_role: str
    verdict: str                     # "approve" | "reject"
    comments: str = ""


@dataclass(kw_only=True)
class VoteCast(BaseEvent):
    """A worker cast a vote (e.g. approve/reject) during swarm resolution."""
    event_type: str = EventType.VOTE_CAST
    agent_id: str
    role: str
    decision: str


@dataclass(kw_only=True)
class SwarmVerified(BaseEvent):
    """The swarm's merged result passed (or failed) final verification."""
    event_type: str = EventType.SWARM_VERIFIED
    status: str                      # "success" | "partial" | "failed"
    issues: str = ""


@dataclass(kw_only=True)
class TaskStatusChanged(BaseEvent):
    """A board task moved to a new status (s12/s17 可观察任务系统)."""
    event_type: str = EventType.TASK_STATUS_CHANGED
    task_id: str
    status: str                      # pending | claimed | done
    owner: str = ""


@dataclass(kw_only=True)
class TaskClaimed(BaseEvent):
    """A worker claimed a pending board task (s17 自主认领)."""
    event_type: str = EventType.TASK_CLAIMED
    task_id: str
    owner: str = ""                  # agent_id that claimed it


@dataclass(kw_only=True)
class TaskReleased(BaseEvent):
    """A claimed task was released back to the board (s17/s16)."""
    event_type: str = EventType.TASK_RELEASED
    task_id: str
    owner: str = ""                  # agent_id that released it


@dataclass(kw_only=True)
class BackgroundResult(BaseEvent):
    """A backgrounded shell command finished (s13)."""
    event_type: str = EventType.BACKGROUND_RESULT
    task_id: str
    success: bool
    stdout: str = ""
    stderr: str = ""


@dataclass(kw_only=True)
class AgentMessage(BaseEvent):
    """Explicit agent-to-agent message (s16 团队协同协议).

    Workers communicate intent over the shared EventBus instead of implicit
    shared state. ``recipient`` is empty for broadcast.
    """
    event_type: str = EventType.AGENT_MESSAGE
    recipient: str = ""                # target agent_id; "" = broadcast
    message: str = ""
    kind: str = "notify"               # notify | request | response
