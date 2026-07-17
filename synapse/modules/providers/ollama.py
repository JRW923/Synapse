"""Ollama LLM Provider implementation.

Ollama exposes an OpenAI-compatible API at http://localhost:11434/v1.
We use the openai SDK with base_url set to that endpoint.
"""

import logging
from openai import AsyncOpenAI
from synapse.protocols.llm import LLMResponse, LLMChunk, Message
from synapse.core.exceptions import ProviderError

logger = logging.getLogger(__name__)


class OllamaProvider:
    """LLM provider backed by a local Ollama instance."""

    def __init__(
        self,
        model: str = "llama3.2",
        base_url: str = "http://localhost:11434/v1",
        api_key: str = "ollama",
        max_tokens: int = 4096,
    ):
        self._model = model
        self._max_tokens = max_tokens
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    @property
    def model_id(self) -> str:
        return self._model

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        converted = self._convert_messages(messages)

        try:
            kwargs: dict = {
                "model": self._model,
                "messages": converted,
                "max_tokens": self._max_tokens,
            }
            if tools:
                kwargs["tools"] = [
                    {
                        "type": "function",
                        "function": {
                            "name": t["name"],
                            "description": t["description"],
                            "parameters": t.get("input_schema", t.get("parameters", {})),
                        },
                    }
                    for t in tools
                ]

            response = await self._client.chat.completions.create(**kwargs)
            return self._parse_response(response)
        except Exception as e:
            raise ProviderError(f"Ollama API error: {e}") from e

    async def stream(self, messages: list[Message], tools: list[dict] | None = None):
        """Streaming not implemented in Phase 1 MVP."""
        raise NotImplementedError("Streaming will be added in Phase 2")

    def _convert_messages(self, messages: list[Message]) -> list[dict]:
        """Convert internal Message list to OpenAI-compatible dicts.

        System messages are passed inline as a normal "system" role
        message, which Ollama's OpenAI-compatible endpoint supports.
        """
        return [{"role": msg.role, "content": msg.content} for msg in messages]

    def _parse_response(self, response) -> LLMResponse:
        """Parse OpenAI-style chat completion into our LLMResponse format."""
        choice = response.choices[0]
        msg = choice.message

        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": tc.function.arguments,  # JSON string
                })

        return LLMResponse(
            content=msg.content or "",
            tool_calls=tool_calls,
            stop_reason=choice.finish_reason or "stop",
            usage={
                "input": response.usage.prompt_tokens,
                "output": response.usage.completion_tokens,
            },
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass
