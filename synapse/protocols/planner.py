"""Planner Protocol."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class PlanningMode(str, Enum):
    REACT = "react"
    PLAN_EXECUTE = "plan_execute"
    HIERARCHICAL = "hierarchical"
    SWARM = "swarm"


class ResultStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass
class ExecutionMetrics:
    tokens_input: int = 0
    tokens_output: int = 0
    tool_call_count: int = 0
    tool_success_count: int = 0
    duration_ms: int = 0
    thrashing_events: int = 0
    # LLM call accounting — separates provider latency from tool time in the
    # efficiency dimension, and lets evaluation classify provider outages as
    # infrastructure failures instead of model failures.
    llm_call_count: int = 0
    llm_time_ms: int = 0
    llm_failure: str = ""  # "" | "provider_unavailable" | "auth" | "llm_error"


@dataclass
class Artifact:
    path: str
    content: str
    action: str  # "created" | "modified" | "deleted"


@dataclass
class AgentResult:
    status: ResultStatus
    output: str
    artifacts: list[Artifact] = field(default_factory=list)
    metrics: ExecutionMetrics = field(default_factory=ExecutionMetrics)
    # Swarm/Team: attribution +溯源 for merge/vote.
    agent_id: str = ""
    role: str = ""
    contributors: list["AgentResult"] = field(default_factory=list)


class Planner(Protocol):
    """Planning strategy for task execution."""

    @property
    def mode(self) -> PlanningMode: ...

    async def execute(
        self,
        task: str,
        context,       # Context from protocols/retriever.py
        tools,         # ToolRegistry from protocols/tool.py
        llm,           # LLMProvider from protocols/llm.py
        sandbox,       # Sandbox from protocols/sandbox.py
        session,       # Session from core/session.py (not a protocol — concrete type)
        event_bus,     # EventBus from core/events.py (not a protocol — concrete type)
    ) -> AgentResult: ...
