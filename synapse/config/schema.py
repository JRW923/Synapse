"""Configuration schema for Synapse."""

from pydantic import BaseModel, Field


class ProviderConfig(BaseModel):
    """LLM provider configuration."""
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    api_key: str = ""
    max_retries: int = 3
    timeout_seconds: int = 120
    max_tokens: int = 4096


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
