"""Provider routing smoke checks for DeepSeek's Anthropic-compatible endpoint."""

from unittest.mock import patch

from synapse.adapters.library import Synapse, _resolve_provider


def test_deepseek_v4_provider_smoke_routes_and_disables_cache():
    provider_cls, base_url = _resolve_provider("deepseek", model="deepseek-v4-flash")
    assert provider_cls.__name__ == "AnthropicProvider"
    assert base_url.endswith("/anthropic")
    with patch("synapse.modules.providers.anthropic.AnthropicProvider") as mocked:
        Synapse(provider="deepseek", model="deepseek-v4-flash", api_key="sk-smoke")
    kwargs = mocked.call_args.kwargs
    assert kwargs["base_url"].endswith("/anthropic")
    assert kwargs["prompt_caching"] is False
