"""DeepSeek LLM Provider implementation.

DeepSeek uses an OpenAI-compatible API, so we use the OpenAI SDK
configured with DeepSeek's base URL (https://api.deepseek.com/v1).
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
        model: str = "deepseek-chat",
        api_key: str = "",
        max_tokens: int = 4096,
        base_url: str = DEEPSEEK_BASE_URL,
    ):
        self._model = model
        self._max_tokens = max_tokens
        self._base_url = base_url
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

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

        Filters out empty user messages (tool-result placeholders).
        """
        result = []
        for msg in messages:
            if msg.role == "user" and msg.content == "":
                continue
            result.append({"role": msg.role, "content": msg.content})
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
