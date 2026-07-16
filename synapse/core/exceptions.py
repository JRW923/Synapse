"""Core exception hierarchy for Synapse."""


class SynapseError(Exception):
    """Base for all Synapse exceptions."""
    pass


class ConfigError(SynapseError):
    """Configuration is invalid at startup — fast-fail, never enter agent loop."""
    pass


class ProviderError(SynapseError):
    """LLM API error — rate limit, timeout, auth failure."""
    pass


class ToolError(SynapseError):
    """Tool execution failed — returned to LLM as ToolResult(success=False)."""
    pass


class SandboxError(SynapseError):
    """Sandbox violation — intercepted by security layer, not shown to LLM."""
    pass


class PlannerError(SynapseError):
    """Planner failure — loop exceeded, sub-task deadlock."""
    pass
