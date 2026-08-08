"""Tests for config loader env-var overrides."""
import json

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


def test_models_json_supplies_default_and_env_can_override_key(tmp_path, monkeypatch):
    models_file = tmp_path / "models.json"
    models_file.write_text(
        json.dumps(
            {
                "version": 1,
                "defaultProvider": "deepseek",
                "defaultModel": "deepseek-chat",
                "providers": {
                    "deepseek": {
                        "apiKey": "sk-file",
                        "models": [{"id": "deepseek-chat"}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")

    config, source = load_config(
        str(tmp_path / "missing.yaml"), models_path=models_file,
    )

    assert config.provider.provider == "deepseek"
    assert config.provider.model == "deepseek-chat"
    assert config.provider.api_key == "sk-env"
    assert source == str(models_file)


def test_models_json_registers_custom_openai_compatible_provider(tmp_path):
    models_file = tmp_path / "models.json"
    models_file.write_text(
        json.dumps(
            {
                "version": 1,
                "defaultProvider": "local",
                "defaultModel": "coder",
                "providers": {
                    "local": {
                        "baseUrl": "http://127.0.0.1:1234/v1",
                        "protocol": "openai",
                        "models": [{"id": "coder"}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    config, _ = load_config(models_path=models_file)

    assert config.provider.provider == "local"
    assert config.provider.base_url == "http://127.0.0.1:1234/v1"
    assert config.provider.custom_providers[0].name == "local"
