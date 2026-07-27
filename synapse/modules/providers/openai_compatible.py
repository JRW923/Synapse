"""Shared logic for OpenAI-compatible LLM providers.

OpenAI, DeepSeek and Ollama all speak the OpenAI Chat Completions wire
format, so their ``chat`` / ``stream`` / message conversion are identical.
This base class holds that shared behaviour; concrete providers only
supply their defaults (model / base_url / api_key) and any response
parsing quirks.

ponytail: this exists purely to delete ~400 lines of copy-pasted provider
code — subclasses are thin.  If a provider diverges in wire format (e.g.
Gemini), do NOT force it into this base.
"""

import json
import logging
from openai import AsyncOpenAI
from synapse.protocols.llm import LLMResponse, LLMChunk, Message
from synapse.core.exceptions import ProviderError

logger = logging.getLogger(__name__)


class OpenAICompatibleProvider:
    """Base for providers behind the OpenAI Chat Completions API."""

    # Subclasses override for clearer error messages.
    _error_prefix: str = "OpenAI-compatible"

    def __init__(
        self,
        model: str = "",
        api_key: str = "",
        max_tokens: int = 4096,
        timeout_seconds: int = 120,
        base_url: str = "",
        client: AsyncOpenAI | None = None,
    ):
        self._model = model
        self._max_tokens = max_tokens
        self._base_url = base_url
        self._encoding = None  # tiktoken cache, lazily built on first use
        if client is not None:
            self._client = client
        else:
            kwargs = dict(api_key=api_key if api_key else None, timeout=timeout_seconds)
            if base_url:
                kwargs["base_url"] = base_url
            self._client = AsyncOpenAI(**kwargs)

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
            raise ProviderError(f"{self._error_prefix} API error: {e}") from e

    async def stream(self, messages: list[Message], tools: list[dict] | None = None):
        """Stream chat completions as a sequence of LLMChunk deltas."""
        converted = self._convert_messages(messages)
        openai_tools = self._convert_tools(tools) if tools else None

        kwargs: dict = {
            "model": self._model,
            "messages": converted,
            "max_tokens": self._max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if openai_tools:
            kwargs["tools"] = openai_tools

        try:
            out_tokens = 0
            async for chunk in self._client.chat.completions.create(**kwargs):
                # Authoritative usage. Standard OpenAI only puts this on the
                # final chunk (stream_options.include_usage); some compatible
                # servers (vLLM, llama.cpp, Ollama native, …) stream cumulative
                # usage per chunk. Prefer it over tiktoken whenever present, so
                # the CLI reconciles against the real total at the end.
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    yield LLMChunk(usage={
                        "input": usage.prompt_tokens or 0,
                        "output": usage.completion_tokens or 0,
                    })
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                content = delta.content or ""
                reasoning = getattr(delta, "reasoning_content", None) or ""
                tool_delta = None
                args_text = ""
                if delta.tool_calls:
                    tc = delta.tool_calls[0]
                    args_text = (tc.function.arguments if tc.function else None) or ""
                    tool_delta = {
                        "index": tc.index,
                        "id": tc.id,
                        "name": tc.function.name if tc.function else None,
                        "input": tc.function.arguments if tc.function else None,
                    }
                # Live output counting via tiktoken for the common case where the
                # server only reports usage on the final chunk. We cumulatively
                # count every streamed text delta (content, tool-call arguments
                # and DeepSeek reasoning_content) so the CLI ticks up smoothly
                # instead of jumping once at the end. Skipped when the server
                # already gave authoritative usage for this chunk.
                usage_payload = None
                if usage is None:
                    piece = content + args_text + reasoning
                    if piece:
                        n = self._count_tokens(piece)
                        if n:
                            out_tokens += n
                            usage_payload = {"input": 0, "output": out_tokens}
                if content or tool_delta:
                    yield LLMChunk(content=content, tool_call_delta=tool_delta, usage=usage_payload)
                elif usage_payload is not None:
                    yield LLMChunk(usage=usage_payload)
        except Exception as e:
            raise ProviderError(f"{self._error_prefix} streaming error: {e}") from e

    def _get_encoding(self):
        """Resolve a tiktoken encoding for this provider's model.

        ponytail: DeepSeek's tokenizer isn't known to tiktoken, so we use
        o200k_base (their documented approximation). Unknown OpenAI-ish models
        fall back to cl100k_base. Cached after first build.
        """
        if self._encoding is None:
            import tiktoken
            model = (self._model or "").lower()
            if "deepseek" in model:
                self._encoding = tiktoken.get_encoding("o200k_base")
            else:
                try:
                    self._encoding = tiktoken.encoding_for_model(self._model)
                except KeyError:
                    self._encoding = tiktoken.get_encoding("cl100k_base")
        return self._encoding

    def _count_tokens(self, text: str) -> int:
        """tiktoken token count for ``text``; 0 if tiktoken is unavailable."""
        try:
            return len(self._get_encoding().encode(text))
        except Exception:
            return 0

    def _convert_messages(self, messages: list[Message]) -> list[dict]:
        """Convert internal Message list to OpenAI-compatible dicts.

        Handles tool_calls (assistant) and tool results, and drops the
        legacy empty user-message placeholder.
        """
        result = []
        for msg in messages:
            if msg.role == "user" and msg.content == "" and not msg.tool_call_id:
                continue

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

    def _convert_tools(self, tools: list[dict]) -> list[dict]:
        """Convert Synapse tool dicts to OpenAI function-calling format.

        Tools carry ``input_schema`` (the internal convention, see
        registry.py); fall back to ``parameters`` for external callers.
        """
        return [
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

    def _parse_response(self, response) -> LLMResponse:
        """Parse an OpenAI-style completion into our LLMResponse.

        Subclasses with different response semantics override this.
        """
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
