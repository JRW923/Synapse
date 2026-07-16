"""LLM Provider Protocol."""

from dataclasses import dataclass, field
from typing import Protocol, AsyncIterator


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[dict] = field(default_factory=list)
    stop_reason: str = "end_turn"  # end_turn | tool_use | max_tokens
    usage: dict[str, int] = field(default_factory=dict)  # {"input": N, "output": M}


@dataclass
class LLMChunk:
    content: str = ""
    tool_call_delta: dict | None = None


class LLMProvider(Protocol):
    """Unified interface for LLM API calls."""

    @property
    def model_id(self) -> str: ...

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
    ) -> LLMResponse: ...

    async def stream(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[LLMChunk]: ...
