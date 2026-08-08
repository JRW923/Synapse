"""User-level LLM model registry stored in ``~/.synapse/models.json``."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from synapse.core.exceptions import ConfigError


class StoredModel(BaseModel):
    """One model ID exposed by a configured provider."""

    id: str = Field(min_length=1)


class StoredProvider(BaseModel):
    """Credentials and endpoint shared by a provider's models."""

    model_config = ConfigDict(populate_by_name=True)

    api_key: str = Field(default="", alias="apiKey")
    base_url: str = Field(default="", alias="baseUrl")
    protocol: Literal["openai", "anthropic"] = "openai"
    models: list[StoredModel] = Field(min_length=1)


class ModelsConfig(BaseModel):
    """Versioned on-disk schema for model registrations and default choice."""

    model_config = ConfigDict(populate_by_name=True)

    version: Literal[1] = 1
    default_provider: str = Field(alias="defaultProvider", min_length=1)
    default_model: str = Field(alias="defaultModel", min_length=1)
    providers: dict[str, StoredProvider] = Field(min_length=1)

    @model_validator(mode="after")
    def _default_is_registered(self) -> "ModelsConfig":
        provider = self.providers.get(self.default_provider)
        if provider is None:
            raise ValueError("defaultProvider must exist in providers")
        if self.default_model not in {model.id for model in provider.models}:
            raise ValueError("defaultModel must exist under defaultProvider")
        return self


def models_config_path() -> Path:
    """Return the single user-level model configuration path."""
    return Path.home() / ".synapse" / "models.json"


def load_models_config(path: str | Path | None = None) -> ModelsConfig | None:
    """Load and validate the model registry, or ``None`` on first run."""
    target = Path(path) if path is not None else models_config_path()
    if not target.exists():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        return ModelsConfig.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ConfigError(f"模型配置无效：{target}\n{exc}") from exc


def save_models_config(config: ModelsConfig, path: str | Path | None = None) -> Path:
    """Atomically persist a validated model registry."""
    target = Path(path) if path is not None else models_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        config.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, target)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


def upsert_model(
    provider: str,
    model: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    protocol: Literal["openai", "anthropic"] = "openai",
    make_default: bool = True,
    path: str | Path | None = None,
) -> ModelsConfig:
    """Add or update one model and optionally make it the persisted default."""
    provider = provider.strip().lower()
    model = model.strip()
    if not provider or not model:
        raise ConfigError("provider 和 model 不能为空")

    config = load_models_config(path)
    if config is None:
        config = ModelsConfig(
            default_provider=provider,
            default_model=model,
            providers={
                provider: StoredProvider(
                    api_key=api_key or "",
                    base_url=base_url or "",
                    protocol=protocol,
                    models=[StoredModel(id=model)],
                )
            },
        )
    else:
        stored = config.providers.get(provider)
        if stored is None:
            stored = StoredProvider(
                api_key=api_key or "",
                base_url=base_url or "",
                protocol=protocol,
                models=[StoredModel(id=model)],
            )
            config.providers[provider] = stored
        else:
            if api_key is not None:
                stored.api_key = api_key
            if base_url is not None:
                stored.base_url = base_url
            stored.protocol = protocol
            if model not in {entry.id for entry in stored.models}:
                stored.models.append(StoredModel(id=model))
        if make_default:
            config.default_provider = provider
            config.default_model = model

    save_models_config(config, path)
    return config


def set_default_model(
    provider: str,
    model: str,
    *,
    path: str | Path | None = None,
) -> ModelsConfig:
    """Persist an already-registered model as the default."""
    config = load_models_config(path)
    if config is None:
        raise ConfigError("尚未创建 models.json，请先添加模型")
    stored = config.providers.get(provider)
    if stored is None or model not in {entry.id for entry in stored.models}:
        raise ConfigError(f"模型未注册：{provider}/{model}")
    config.default_provider = provider
    config.default_model = model
    save_models_config(config, path)
    return config


def apply_model_selection(config, provider: str, model: str) -> None:
    """Select a configured model and update its credentials on runtime config."""
    from synapse.config.schema import _effective_api_key

    previous_provider = config.provider.provider
    previous_key = config.provider.api_key
    previous_base_url = config.provider.base_url
    config.provider.provider = provider
    config.provider.model = model
    config.provider.api_key = previous_key if provider == previous_provider else ""
    config.provider.base_url = previous_base_url if provider == previous_provider else ""

    for entry in config.provider.models:
        if entry.provider == provider and entry.model == model:
            config.provider.api_key = _effective_api_key(entry) or config.provider.api_key
            config.provider.base_url = entry.base_url or config.provider.base_url
            return
    for custom in config.provider.custom_providers:
        if custom.name == provider and model in custom.models:
            config.provider.api_key = custom.api_key
            config.provider.base_url = custom.base_url
            return


def apply_models_config(config, registry: ModelsConfig) -> None:
    """Overlay model registrations onto the existing non-LLM YAML config."""
    from synapse.config.schema import (
        CustomProvider,
        ModelEntry,
        _PROVIDER_ENV_VARS,
    )

    models: list[ModelEntry] = []
    custom_providers: list[CustomProvider] = []
    for name, provider in registry.providers.items():
        ids = [model.id for model in provider.models]
        if name in _PROVIDER_ENV_VARS:
            models.extend(
                ModelEntry(
                    provider=name,
                    model=model_id,
                    api_key=provider.api_key,
                    base_url=provider.base_url,
                )
                for model_id in ids
            )
        else:
            custom_providers.append(
                CustomProvider(
                    name=name,
                    base_url=provider.base_url,
                    api_key=provider.api_key,
                    protocol=provider.protocol,
                    models=ids,
                )
            )

    config.provider.models = models
    config.provider.custom_providers = custom_providers
    selected = registry.providers[registry.default_provider]
    config.provider.provider = registry.default_provider
    config.provider.model = registry.default_model
    config.provider.api_key = selected.api_key
    config.provider.base_url = selected.base_url
