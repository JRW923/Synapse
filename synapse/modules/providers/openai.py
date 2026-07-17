"""OpenAI LLM Provider implementation."""

import json
import logging
from openai import AsyncOpenAI
from synapse.protocols.llm import LLMResponse, LLMChunk, Message
from synapse.core.exceptions import ProviderError

logger = logging.getLogger(__name__)


class OpenAIProvider:
    """LLM provider backed by OpenAI's API."""

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: str = "",
        max_tokens: int = 4096,
        timeout_seconds: int = 120,
    ):
        self._model = model
        self._max_tokens = max_tokens
        self._client = AsyncOpenAI(
            api_key=api_key if api_key else None,
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
        openai_tools = self._convert_tools(tools) if tools else None

        try:
            kwargs = {
                "model": self._model,
                "messages": converted,
                "max_tokens": self._max_tokens,
            }
            if openai_tools:
                kwargs["tools"] = openai_tools

            response = await self._client.chat.completions.create(**kwargs)
            return self._parse_response(response)
        except Exception as e:
            raise ProviderError(f"OpenAI API error: {e}") from e

    async def stream(self, messages: list[Message], tools: list[dict] | None = None):
        """Streaming not implemented in Phase 2 MVP."""
        raise NotImplementedError("Streaming will be added in a later phase")

    def _convert_messages(self, messages: list[Message]) -> list[dict]:
        """Convert internal Message to OpenAI API format.

        OpenAI includes system messages directly in the messages list
        (unlike Anthropic which uses a separate system parameter).
        """
        result = []
        for msg in messages:
            if msg.role == "user" and msg.content == "" and not msg.tool_call_id:
                # Legacy tool result placeholder — skip
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

    def _convert_tools(self, tools: list[dict]) -> list[dict]:
        """Convert tools to OpenAI's function-calling format.

        OpenAI tool format:
            {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t.get("parameters", t.get("input_schema", {})),
                },
            }
            for t in tools
        ]

    def _parse_response(self, response) -> LLMResponse:
        """Parse OpenAI response into our LLMResponse format."""
        choice = response.choices[0]
        message = choice.message

        text_content = message.content or ""
        tool_calls = []

        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": json.loads(tc.function.arguments),
                })

        # Determine stop reason
        finish_reason = choice.finish_reason or "stop"
        if finish_reason == "tool_calls":
            stop_reason = "tool_use"
        else:
            stop_reason = finish_reason

        return LLMResponse(
            content=text_content,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage={
                "input": response.usage.prompt_tokens,
                "output": response.usage.completion_tokens,
            },
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass
