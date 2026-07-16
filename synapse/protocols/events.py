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
    THRASHING_DETECTED = "thrashing_detected"


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
class ThrashingDetected(BaseEvent):
    event_type: str = EventType.THRASHING_DETECTED
    file_path: str
    modification_count: int
