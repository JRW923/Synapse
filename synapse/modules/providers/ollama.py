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
        timeout_seconds: int = 120,
    ):
        self._model = model
        self._max_tokens = max_tokens
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout_seconds,
        )

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
        """Stream chat completions as a sequence of LLMChunk deltas."""
        converted = self._convert_messages(messages)

        kwargs: dict = {
            "model": self._model,
            "messages": converted,
            "max_tokens": self._max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
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

        usage: dict[str, int] = {}
        try:
            async for chunk in self._client.chat.completions.create(**kwargs):
                if getattr(chunk, "usage", None):
                    usage = {
                        "input": chunk.usage.prompt_tokens or 0,
                        "output": chunk.usage.completion_tokens or 0,
                    }
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                content = delta.content or ""
                tool_delta = None
                if delta.tool_calls:
                    tc = delta.tool_calls[0]
                    tool_delta = {
                        "index": tc.index,
                        "id": tc.id,
                        "name": tc.function.name if tc.function else None,
                        "input": tc.function.arguments if tc.function else None,
                    }
                if content or tool_delta:
                    yield LLMChunk(content=content, tool_call_delta=tool_delta)
            if usage:
                yield LLMChunk(usage=usage)
        except Exception as e:
            raise ProviderError(f"Ollama API error: {e}") from e

    def _convert_messages(self, messages: list[Message]) -> list[dict]:
        """Convert internal Message list to OpenAI-compatible dicts.

        System messages are passed inline as a normal "system" role
        message, which Ollama's OpenAI-compatible endpoint supports.
        """
        result = []
        for msg in messages:
            entry: dict = {"role": msg.role, "content": msg.content}

            if msg.role == "assistant" and msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["input"], ensure_ascii=False),
                        },
                    }
                    for tc in msg.tool_calls
                ]

            if msg.role == "tool" and msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id

            result.append(entry)
        return result

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
