"""Anthropic LLM Provider implementation."""

import logging
import re
from anthropic import AsyncAnthropic
from synapse.protocols.llm import LLMResponse, LLMChunk, Message
from synapse.core.exceptions import ProviderError

logger = logging.getLogger(__name__)

# Pattern to detect tool-result user messages produced by ReActPlanner
# Format: [Tool {tool_use_id} {status}]: {output}\nError: {error}
TOOL_RESULT_RE = re.compile(r'^\[Tool (\S+) (success|failed)\]:')


class AnthropicProvider:
    """LLM provider backed by Anthropic's API."""

    def __init__(self, model: str = "claude-sonnet-4-6", api_key: str = "", max_tokens: int = 4096):
        self._model = model
        self._max_tokens = max_tokens
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
                "max_tokens": self._max_tokens,
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
        """Convert internal Message to Anthropic API format, filtering system.

        Detects tool-result messages (formatted as "[Tool <id> <status>]: ...")
        and converts them to Anthropic's tool_result content block format.
        """
        result = []
        for msg in messages:
            if msg.role == "system":
                continue

            # Detect tool-result user messages from ReActPlanner (I1)
            m = TOOL_RESULT_RE.match(msg.content)
            if m and msg.role == "user":
                tool_use_id = m.group(1)
                # Extract actual result content after the prefix "[Tool {id} {status}]: "
                prefix_end = msg.content.index("]: ") + 3
                result_content = msg.content[prefix_end:]
                # Strip trailing "\nError: ..." suffix
                error_idx = result_content.rfind("\nError: ")
                if error_idx != -1:
                    result_content = result_content[:error_idx]

                result.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": result_content,
                        }
                    ],
                })
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
