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


@dataclass(kw_only=True)
class BaseEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=datetime.now)
    session_id: str


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
