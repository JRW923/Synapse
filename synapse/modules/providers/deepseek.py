"""DeepSeek LLM Provider implementation.

For ``deepseek-chat`` this uses the OpenAI-compatible endpoint at
``api.deepseek.com/v1``.  ``deepseek-v4-*`` models are routed through
the Anthropic protocol at ``api.deepseek.com/anthropic`` (handled in
``_resolve_provider`` in library.py).
"""

import json
import logging
from openai import AsyncOpenAI
from synapse.protocols.llm import LLMResponse, LLMChunk, Message
from synapse.core.exceptions import ProviderError

logger = logging.getLogger(__name__)

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


class DeepSeekProvider:
    """LLM provider backed by DeepSeek's OpenAI-compatible API."""

    def __init__(
        self,
        model: str = "deepseek-v4-pro",
        api_key: str = "",
        max_tokens: int = 4096,
        base_url: str = DEEPSEEK_BASE_URL,
        timeout_seconds: int = 120,
    ):
        import os
        self._model = model
        self._max_tokens = max_tokens
        self._base_url = base_url
        # If no key provided, try env vars (DEEPSEEK_API_KEY first, then OPENAI_API_KEY)
        resolved_key = api_key or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        self._client = AsyncOpenAI(
            api_key=resolved_key,
            base_url=base_url,
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
            raise ProviderError(f"DeepSeek API error: {e}") from e

    async def stream(self, messages: list[Message], tools: list[dict] | None = None):
        """Streaming not implemented in Phase 1 MVP."""
        raise NotImplementedError("Streaming will be added in Phase 2")

    def _convert_messages(self, messages: list[Message]) -> list[dict]:
        """Convert internal Message objects to OpenAI-compatible JSON dicts.

        Handles:
        - assistant messages with tool_calls → OpenAI tool_calls format
        - tool messages → OpenAI tool role with tool_call_id
        - empty user messages (legacy placeholders) are filtered out
        """
        result = []
        for msg in messages:
            if msg.role == "user" and msg.content == "" and not msg.tool_call_id:
                continue

            entry: dict = {"role": msg.role, "content": msg.content}

            # Assistant message with tool_calls
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

            # Tool result message
            if msg.role == "tool" and msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id

            result.append(entry)
        return result

    def _parse_response(self, response) -> LLMResponse:
        """Parse OpenAI-compatible response into our internal LLMResponse."""
        choice = response.choices[0]
        message = choice.message

        text_parts = []
        tool_calls = []

        if message.content:
            text_parts.append(message.content)

        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append(
                    {
                        "id": tc.id,
                        "name": tc.function.name,
                        "input": json.loads(tc.function.arguments),
                    }
                )

        return LLMResponse(
            content="\n".join(text_parts) if text_parts else "",
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
