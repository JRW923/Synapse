"""OpenAI LLM Provider implementation."""

from synapse.modules.providers.openai_compatible import OpenAICompatibleProvider


class OpenAIProvider(OpenAICompatibleProvider):
    """LLM provider backed by OpenAI's API."""

    _error_prefix = "OpenAI"

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: str = "",
        max_tokens: int = 4096,
        timeout_seconds: int = 120,
        base_url: str = "",
    ):
        super().__init__(
            model=model,
            api_key=api_key,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            base_url=base_url,
        )
