"""Anthropic LLM Provider implementation."""

import logging
from anthropic import AsyncAnthropic
from synapse.protocols.llm import LLMResponse, LLMChunk, Message
from synapse.core.exceptions import ProviderError

logger = logging.getLogger(__name__)

class AnthropicProvider:
    """LLM provider backed by Anthropic's API."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        api_key: str = "",
        max_tokens: int = 4096,
        timeout_seconds: int = 120,
        base_url: str = "",
    ):
        self._model = model
        self._max_tokens = max_tokens
        kwargs = dict(api_key=api_key if api_key else None, timeout=timeout_seconds)
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncAnthropic(**kwargs)

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
        """Stream messages as a sequence of LLMChunk deltas (text + tool_use)."""
        system_prompt = self._extract_system(messages)
        converted = self._convert_messages(messages)

        kwargs: dict = {
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

        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    if event.type == "content_block_start":
                        block = event.content_block
                        if getattr(block, "type", None) == "tool_use":
                            yield LLMChunk(tool_call_delta={
                                "index": event.index,
                                "id": getattr(block, "id", None),
                                "name": getattr(block, "name", None),
                            })
                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            if delta.text:
                                yield LLMChunk(content=delta.text)
                        elif delta.type == "input_json_delta":
                            yield LLMChunk(tool_call_delta={"input": delta.partial_json})
                try:
                    final = await stream.get_final_message()
                    if getattr(final, "usage", None):
                        yield LLMChunk(usage={
                            "input": final.usage.input_tokens or 0,
                            "output": final.usage.output_tokens or 0,
                        })
                except Exception:
                    pass
        except Exception as e:
            raise ProviderError(f"Anthropic streaming error: {e}") from e

    def _extract_system(self, messages: list[Message]) -> str | None:
        """Extract system message if present (Anthropic uses separate system param)."""
        for msg in messages:
            if msg.role == "system":
                return msg.content
        return None

    def _convert_messages(self, messages: list[Message]) -> list[dict]:
        """Convert internal Message to Anthropic API format, filtering system.

        Handles:
        - tool messages (role="tool") → Anthropic tool_result blocks
        - consecutive tool messages are merged into one user message
          (Anthropic requires ALL tool_results for an assistant's tool_uses
          to appear in the immediately-following user message)
        - assistant messages with tool_calls → Anthropic tool_use blocks
        """
        # Step 1 — build intermediate list, grouping consecutive tool messages.
        tmp: list[dict] = []
        for msg in messages:
            if msg.role == "system":
                continue
            if msg.role == "tool" and msg.tool_call_id:
                block = {"type": "tool_result", "tool_use_id": msg.tool_call_id, "content": msg.content}
                if (tmp and tmp[-1].get("role") == "user"
                        and isinstance(tmp[-1].get("content"), list)):
                    tmp[-1]["content"].append(block)
                else:
                    tmp.append({"role": "user", "content": [block]})
                continue

            if msg.role == "assistant" and msg.tool_calls:
                blocks = []
                if msg.content:
                    blocks.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]})
                tmp.append({"role": "assistant", "content": blocks})
                continue

            if msg.role == "user" and msg.content == "":
                continue
            tmp.append({"role": msg.role, "content": msg.content})

        # Step 2 — deduplicate tool_result IDs within each merged user message.
        for entry in tmp:
            c = entry.get("content")
            if isinstance(c, list) and c and c[0].get("type") == "tool_result":
                seen: set[str] = set()
                deduped = []
                for block in c:
                    tid = block.get("tool_use_id", "")
                    if tid and tid not in seen:
                        seen.add(tid)
                        deduped.append(block)
                entry["content"] = deduped
        return tmp

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
