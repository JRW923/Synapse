"""Tests for config loader env-var overrides."""
from synapse.config.loader import load_config


def test_env_key_applies_only_to_current_provider(tmp_path, monkeypatch):
    """A GOOGLE_API_KEY must not clobber the key of an anthropic config
    (previously the last provider env var in dict order won, arbitrarily)."""
    config_file = tmp_path / "synapse.yaml"
    config_file.write_text(
        "provider:\n  provider: anthropic\n  model: claude-sonnet-4-6\n  api_key: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic")
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-google")
    config, _ = load_config(str(config_file))
    assert config.provider.api_key == "sk-anthropic"


def test_env_provider_uses_its_own_key(tmp_path, monkeypatch):
    """Switching provider via env picks up that provider's key, not another's."""
    config_file = tmp_path / "synapse.yaml"
    config_file.write_text(
        "provider:\n  provider: deepseek\n  model: x\n  api_key: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SYNAPSE_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    config, _ = load_config(str(config_file))
    assert config.provider.provider == "openai"
    assert config.provider.api_key == "sk-openai"
