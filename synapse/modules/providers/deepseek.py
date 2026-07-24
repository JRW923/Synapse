"""DeepSeek LLM Provider implementation.

For ``deepseek-chat`` this uses the OpenAI-compatible endpoint at
``api.deepseek.com/v1``.  ``deepseek-v4-*`` models are routed through
the Anthropic protocol at ``api.deepseek.com/anthropic`` (handled in
``_resolve_provider`` in library.py).
"""

import os
from synapse.modules.providers.openai_compatible import OpenAICompatibleProvider


DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


class DeepSeekProvider(OpenAICompatibleProvider):
    """LLM provider backed by DeepSeek's OpenAI-compatible API."""

    _error_prefix = "DeepSeek"

    def __init__(
        self,
        model: str = "deepseek-v4-pro",
        api_key: str = "",
        max_tokens: int = 4096,
        base_url: str = DEEPSEEK_BASE_URL,
        timeout_seconds: int = 120,
    ):
        # If no key provided, try env vars (DEEPSEEK_API_KEY first, then OPENAI_API_KEY)
        resolved_key = api_key or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        super().__init__(
            model=model,
            api_key=resolved_key,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            base_url=base_url,
        )

    def _parse_response(self, response) -> "object":
        """Parse OpenAI-compatible response (no tool_use stop-reason mapping)."""
        from synapse.protocols.llm import LLMResponse
        choice = response.choices[0]
        message = choice.message

        text_parts = []
        tool_calls = []

        if message.content:
            text_parts.append(message.content)

        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": __import__("json").loads(tc.function.arguments),
                })

        return LLMResponse(
            content="\n".join(text_parts) if text_parts else "",
            tool_calls=tool_calls,
            stop_reason=choice.finish_reason or "stop",
            usage={
                "input": response.usage.prompt_tokens,
                "output": response.usage.completion_tokens,
            },
        )
