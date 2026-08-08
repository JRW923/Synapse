"""Configuration schema for Synapse."""

import os
from pydantic import BaseModel, Field


class ModelEntry(BaseModel):
    """A pre-configured model entry.  API key can be set here or via env var."""
    provider: str = ""       # anthropic | openai | deepseek | google | ollama
    model: str = ""          # e.g. claude-sonnet-4-6
    api_key: str = ""        # leave empty → check env var
    base_url: str = ""       # override the default API endpoint (custom providers)
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0


class CustomProvider(BaseModel):
    """A user-defined provider with its own base URL and models."""
    name: str = ""           # provider name shown in /provider
    base_url: str = ""       # API endpoint
    api_key: str = ""        # API key
    protocol: str = "openai"  # "openai" or "anthropic" (API style)
    models: list[str] = Field(default_factory=list)  # e.g. ["my-model-1", "my-model-2"]


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
    {"provider": "deepseek",  "model": "deepseek-v4-flash",  "api_key": ""},
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
    base_url: str = ""
    max_retries: int = 3
    timeout_seconds: int = 120
    max_tokens: int = 4096
    fallback_models: list[ModelEntry] = Field(default_factory=list)
    routing: str = "fallback"  # fallback | lowest_cost
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0
    # Model entries are populated from ~/.synapse/models.json when present.
    # Built-in presets remain the compatibility fallback for legacy YAML users.
    models: list[ModelEntry] = Field(default_factory=_default_models)
    # User-defined providers with custom base URLs.
    custom_providers: list[CustomProvider] = Field(default_factory=list)


class ToolsConfig(BaseModel):
    """Tool configuration."""
    enabled: list[str] = Field(default_factory=lambda: [
        "read", "write", "edit", "glob", "grep", "shell", "git",
        "load_skill", "todo_write", "todo_read", "web_search", "web_fetch",
        "web", "db", "browser",
    ])
    allowlist_commands: list[str] = Field(default_factory=lambda: [
        "ls", "git", "pytest", "python", "pip", "npm", "cargo", "go", "node",
        "curl", "wget", "mkdir", "find", "cat", "echo", "type", "dir",
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
    # Cap on tool-output chars fed into the conversation context. ReAct re-sends
    # the full context every iteration, so an unbounded tool result (e.g. a 100
    # KB HTML page) is re-counted each turn and blows the token budget fast.
    # 0 = no cap. This is the universal safety net for ALL tools.
    max_tool_result_chars: int = 16_000


class ContextConfig(BaseModel):
    """Context engineering configuration (Phase E)."""
    # Total token budget for context assembly. 0 = inherit from planning.max_tokens_per_task.
    total_tokens: int = 0
    # Four-zone percentage split. Must sum to ~1.0.
    system_pct: float = 0.15
    core_pct: float = 0.50
    reference_pct: float = 0.25
    overflow_pct: float = 0.10
    # Compaction strategy for OVERFLOW blocks.
    # truncation | llm | off  (llm uses LLMCompactor with fallback to truncation)
    compaction_strategy: str = "truncation"
    # Only invoke LLM compactor when overflow total chars exceed this threshold.
    llm_compact_threshold_chars: int = 1000


class SecurityConfig(BaseModel):
    """Security configuration."""
    sandbox_enabled: bool = True
    sandbox_mode: str = "enforce"  # enforce | warn | off
    sandbox_backend: str = "process"  # process | auto | bubblewrap | seatbelt | docker
    sandbox_network: bool = False
    sandbox_docker_image: str = "python:3.12-slim"
    auth_confirmation: bool = True  # require user confirmation for risky ops
    # Empty means the workspace boundary is the default; explicit entries
    # enable narrower per-worker file scopes.
    allowed_paths: list[str] = Field(default_factory=list)
    # Gate for RiskLevel.EXTERNAL tools (web, browser, db). False by default so
    # the heavier external tools stay blocked unless explicitly opted in;
    # web_search is READ_ONLY and works regardless of this switch.
    allow_external: bool = False


class HooksConfig(BaseModel):
    """User lifecycle hooks (s04).

    Maps an event type (the ``EventType`` string, e.g. ``tool_call_completed``)
    to a list of shell commands run after that event fires.  Payload is passed
    via the ``SYNAPSE_EVENT`` / ``SYNAPSE_PAYLOAD`` env vars.  Start with
    read-only PostToolUse hooks; PreToolUse blocking is future work.
    """
    hooks: dict[str, list[str]] = Field(default_factory=dict)


class PluginsConfig(BaseModel):
    """Directories or manifest files discovered at startup."""
    paths: list[str] = Field(default_factory=list)


class SynapseConfig(BaseModel):
    """Root configuration."""
    provider: ProviderConfig = ProviderConfig()
    tools: ToolsConfig = ToolsConfig()
    planning: PlanningConfig = PlanningConfig()
    security: SecurityConfig = SecurityConfig()
    context: ContextConfig = ContextConfig()
    hooks: HooksConfig = HooksConfig()
    plugins: PluginsConfig = PluginsConfig()
