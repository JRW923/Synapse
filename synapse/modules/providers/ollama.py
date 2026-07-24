"""Ollama LLM Provider implementation.

Ollama exposes an OpenAI-compatible API at http://localhost:11434/v1.
We use the openai SDK with base_url set to that endpoint.
"""

from synapse.modules.providers.openai_compatible import OpenAICompatibleProvider


class OllamaProvider(OpenAICompatibleProvider):
    """LLM provider backed by a local Ollama instance."""

    _error_prefix = "Ollama"

    def __init__(
        self,
        model: str = "llama3.2",
        base_url: str = "http://localhost:11434/v1",
        api_key: str = "ollama",
        max_tokens: int = 4096,
        timeout_seconds: int = 120,
    ):
        super().__init__(
            model=model,
            api_key=api_key,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            base_url=base_url,
        )

    def _parse_response(self, response) -> "object":
        """Parse OpenAI-style completion (raw argument string, no mapping)."""
        from synapse.protocols.llm import LLMResponse
        choice = response.choices[0]
        msg = choice.message

        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": tc.function.arguments,  # JSON string, left unparsed
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
