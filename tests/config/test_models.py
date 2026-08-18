import json

import pytest

from synapse.config.models import (
    ModelsConfig,
    StoredModel,
    StoredProvider,
    apply_model_selection,
    load_models_config,
    save_models_config,
    set_default_model,
    upsert_model,
)
from synapse.core.exceptions import ConfigError


def test_missing_models_file_is_first_run(tmp_path):
    assert load_models_config(tmp_path / "models.json") is None


def test_models_file_round_trip_uses_user_facing_keys(tmp_path):
    path = tmp_path / "models.json"
    registry = ModelsConfig(
        default_provider="deepseek",
        default_model="deepseek-v4-pro",
        providers={
            "deepseek": StoredProvider(
                api_key="sk-test",
                models=[StoredModel(id="deepseek-v4-pro")],
            )
        },
    )

    save_models_config(registry, path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["defaultProvider"] == "deepseek"
    assert raw["providers"]["deepseek"]["apiKey"] == "sk-test"
    assert load_models_config(path) == registry


def test_upsert_model_deduplicates_and_persists_default(tmp_path):
    path = tmp_path / "models.json"
    upsert_model("openai", "gpt-5.4", api_key="sk-one", path=path)
    upsert_model("openai", "gpt-5.4", api_key="sk-two", path=path)
    upsert_model("openai", "o4-mini", path=path, make_default=False)
    set_default_model("openai", "o4-mini", path=path)

    registry = load_models_config(path)
    assert registry is not None
    assert [model.id for model in registry.providers["openai"].models] == [
        "gpt-5.4",
        "o4-mini",
    ]
    assert registry.providers["openai"].api_key == "sk-two"
    assert registry.default_model == "o4-mini"


def test_model_selection_uses_the_selected_providers_credentials(tmp_path):
    from synapse.config.loader import load_config

    path = tmp_path / "models.json"
    upsert_model("openai", "gpt-5.4", api_key="sk-openai", path=path)
    upsert_model(
        "deepseek", "deepseek-v4-pro", api_key="sk-deepseek",
        path=path, make_default=False,
    )
    config, _ = load_config(
        str(tmp_path / "missing.yaml"), models_path=path,
    )

    apply_model_selection(config, "deepseek", "deepseek-v4-pro")

    assert config.provider.api_key == "sk-deepseek"


def test_invalid_models_file_reports_its_path(tmp_path):
    path = tmp_path / "models.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ConfigError, match="models.json"):
        load_models_config(path)


def test_default_must_reference_a_registered_model(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "defaultProvider": "openai",
                "defaultModel": "missing",
                "providers": {"openai": {"models": [{"id": "gpt-5.4"}]}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="defaultModel"):
        load_models_config(path)
