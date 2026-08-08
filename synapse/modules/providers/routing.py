"""Provider failover and simple cost-aware routing."""

from __future__ import annotations

from collections.abc import AsyncIterator

from synapse.protocols.llm import LLMChunk, LLMResponse, Message


class FallbackLLMProvider:
    """Try providers in order; do not duplicate a partially streamed response."""

    def __init__(self, providers: list, costs: list[float] | None = None):
        if not providers:
            raise ValueError("at least one provider is required")
        self.providers = providers
        self.costs = costs or [0.0] * len(providers)
        self._active = 0

    @property
    def model_id(self) -> str:
        return self.providers[self._active].model_id

    async def chat(self, messages: list[Message], tools: list[dict] | None = None) -> LLMResponse:
        last: Exception | None = None
        for index, provider in enumerate(self.providers):
            try:
                response = await provider.chat(messages, tools=tools)
                self._active = index
                return response
            except Exception as exc:
                last = exc
        raise RuntimeError(f"all LLM providers failed: {last}") from last

    async def stream(
        self, messages: list[Message], tools: list[dict] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        last: Exception | None = None
        for index, provider in enumerate(self.providers):
            yielded = False
            try:
                async for chunk in provider.stream(messages, tools=tools):
                    yielded = True
                    yield chunk
                self._active = index
                return
            except Exception as exc:
                last = exc
                if yielded:
                    raise
        raise RuntimeError(f"all LLM providers failed: {last}") from last
