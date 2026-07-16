"""Anthropic LLM Provider implementation."""

import logging
from anthropic import AsyncAnthropic
from synapse.protocols.llm import LLMResponse, LLMChunk, Message
from synapse.core.exceptions import ProviderError

logger = logging.getLogger(__name__)


class AnthropicProvider:
    """LLM provider backed by Anthropic's API."""

    def __init__(self, model: str = "claude-sonnet-4-6", api_key: str = ""):
        self._model = model
        self._client = AsyncAnthropic(api_key=api_key) if api_key else AsyncAnthropic()

    @property
    def model_id(self) -> str:
        return self._model

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        system_prompt = self._extract_system(messages)
        converted = self._convert_messages(messages)

        try:
            kwargs = {
                "model": self._model,
                "messages": converted,
                "max_tokens": 4096,
            }
            if system_prompt:
                kwargs["system"] = system_prompt
            if tools:
                kwargs["tools"] = [
                    {
                        "name": t["name"],
                        "description": t["description"],
                        "input_schema": t.get("input_schema", t.get("parameters", {})),
                    }
                    for t in tools
                ]

            response = await self._client.messages.create(**kwargs)
            return self._parse_response(response)
        except Exception as e:
            raise ProviderError(f"Anthropic API error: {e}") from e

    async def stream(self, messages: list[Message], tools: list[dict] | None = None):
        """Streaming not implemented in Phase 1 MVP."""
        raise NotImplementedError("Streaming will be added in Phase 2")

    def _extract_system(self, messages: list[Message]) -> str | None:
        """Extract system message if present (Anthropic uses separate system param)."""
        for msg in messages:
            if msg.role == "system":
                return msg.content
        return None

    def _convert_messages(self, messages: list[Message]) -> list[dict]:
        """Convert internal Message to Anthropic API format, filtering system."""
        result = []
        for msg in messages:
            if msg.role == "system":
                continue
            if msg.role == "user" and msg.content == "":
                # Tool result placeholder
                continue
            result.append({"role": msg.role, "content": msg.content})
        return result

    def _parse_response(self, response) -> LLMResponse:
        """Parse Anthropic response into our LLMResponse format."""
        text_parts = []
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })

        return LLMResponse(
            content="\n".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=response.stop_reason or "end_turn",
            usage={
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
            },
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass
