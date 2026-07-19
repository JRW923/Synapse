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

        # DEBUG: check for duplicate tool_result IDs
        import sys as _sd
        for _mi, _m in enumerate(converted):
            _c = _m.get("content","")
            if isinstance(_c, list):
                _ids = [b.get("tool_use_id","") for b in _c if b.get("type")=="tool_result"]
                if len(_ids) != len(set(_ids)):
                    _sd.stderr.write(f"[DEBUG] DUPLICATE tool_result ids in msg {_mi}: {_ids}\n")
                    _sd.stderr.flush()
                _tids = [b.get("id","") for b in _c if b.get("type")=="tool_use"]
                if len(_tids) != len(set(_tids)):
                    _sd.stderr.write(f"[DEBUG] DUPLICATE tool_use ids in msg {_mi}: {_tids}\n")
                    _sd.stderr.flush()

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

        Handles:
        - tool messages (role="tool") → Anthropic tool_result blocks
        - consecutive tool messages are merged into one user message
          (Anthropic requires ALL tool_results for an assistant's tool_uses
          to appear in the immediately-following user message)
        - assistant messages with tool_calls → Anthropic tool_use blocks
        """
        result = []
        for msg in messages:
            if msg.role == "system":
                continue

            # tool message → user message with tool_result block(s).
            # If the previous entry is already a user-tool_result, append to it
            # so that consecutive tool messages merge into one user message
            # (required by Anthropic: all tool_results for an assistant's
            #  tool_uses must appear in the immediately-following message).
            if msg.role == "tool" and msg.tool_call_id:
                if (result and isinstance(result[-1], dict)
                        and result[-1].get("role") == "user"
                        and isinstance(result[-1].get("content"), list)):
                    result[-1]["content"].append({
                        "type": "tool_result",
                        "tool_use_id": msg.tool_call_id,
                        "content": msg.content,
                    })
                else:
                    result.append({
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.tool_call_id,
                                "content": msg.content,
                            }
                        ],
                    })
                continue

            # assistant message with tool_calls → tool_use blocks
            if msg.role == "assistant" and msg.tool_calls:
                content_blocks = []
                if msg.content:
                    content_blocks.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": tc["input"],
                    })
                result.append({"role": "assistant", "content": content_blocks})
                continue

            if msg.role == "user" and msg.content == "":
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
