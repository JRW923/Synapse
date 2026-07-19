"""Configuration schema for Synapse."""

import os
from pydantic import BaseModel, Field


class ModelEntry(BaseModel):
    """A pre-configured model entry.  API key can be set here or via env var."""
    provider: str = ""       # anthropic | openai | deepseek | google | ollama
    model: str = ""          # e.g. claude-sonnet-4-6
    api_key: str = ""        # leave empty → check env var


# ---- built-in defaults — API keys empty; fill in or set env vars -----------

_DEFAULT_MODELS: list[dict] = [
    {"provider": "anthropic", "model": "claude-sonnet-4-6",  "api_key": ""},
    {"provider": "anthropic", "model": "claude-opus-4-7",    "api_key": ""},
    {"provider": "anthropic", "model": "claude-haiku-4-5",   "api_key": ""},
    {"provider": "openai",    "model": "gpt-5.5",            "api_key": ""},
    {"provider": "openai",    "model": "gpt-5.4",            "api_key": ""},
    {"provider": "openai",    "model": "o4-mini",            "api_key": ""},
    {"provider": "deepseek",  "model": "deepseek-chat",      "api_key": ""},
    {"provider": "deepseek",  "model": "deepseek-v4-pro",    "api_key": ""},
    {"provider": "google",    "model": "gemini-3-flash",     "api_key": ""},
    {"provider": "google",    "model": "gemini-3-pro",       "api_key": ""},
    {"provider": "ollama",    "model": "qwen3.5:4b",         "api_key": ""},
    {"provider": "ollama",    "model": "llama4:8b",          "api_key": ""},
]

# env var name for each provider
_PROVIDER_ENV_VARS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "google": "GOOGLE_API_KEY",
    "ollama": "",  # Ollama runs locally, no key needed
}


def _effective_api_key(entry: "ModelEntry") -> str:
    """Return the effective API key: explicit value first, then env var."""
    if entry.api_key:
        return entry.api_key
    env_var = _PROVIDER_ENV_VARS.get(entry.provider, "")
    if env_var:
        return os.environ.get(env_var, "")
    return ""  # ollama


def _default_models() -> list[ModelEntry]:
    return [ModelEntry(**d) for d in _DEFAULT_MODELS]


class ProviderConfig(BaseModel):
    """LLM provider configuration."""
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    api_key: str = ""
    max_retries: int = 3
    timeout_seconds: int = 120
    max_tokens: int = 4096
    # Pre-configured models shown by /model.  Add entries to ~/.synapse/config.yaml
    # to fill in api_key values (or set the corresponding env var).
    models: list[ModelEntry] = Field(default_factory=_default_models)


class ToolsConfig(BaseModel):
    """Tool configuration."""
    enabled: list[str] = Field(default_factory=lambda: [
        "read", "write", "edit", "glob", "grep", "shell", "git"
    ])
    allowlist_commands: list[str] = Field(default_factory=lambda: [
        "ls", "git", "pytest", "python", "pip", "npm", "cargo", "go", "node"
    ])
    workspace_root: str = "."


class PlanningConfig(BaseModel):
    """Planner configuration."""
    mode: str = "react"  # react | plan_execute | hierarchical
    max_iterations: int = 50
    thrashing_threshold: int = 3         # file touch count to detect thrashing
    max_thrashing_events: int = 2        # stop when this many thrashing events fire
    max_tokens_per_task: int = 200_000   # stop task when total tokens exceed this
    total_timeout_seconds: int = 300


class SecurityConfig(BaseModel):
    """Security configuration."""
    sandbox_enabled: bool = True
    sandbox_mode: str = "enforce"  # enforce | warn | off
    auth_confirmation: bool = True  # require user confirmation for risky ops
    allowed_paths: list[str] = Field(default_factory=lambda: ["."])


class SynapseConfig(BaseModel):
    """Root configuration."""
    provider: ProviderConfig = ProviderConfig()
    tools: ToolsConfig = ToolsConfig()
    planning: PlanningConfig = PlanningConfig()
    security: SecurityConfig = SecurityConfig()
