"""Minimal custom Tool — proves tool-level extensibility.

A tool is any class with the ``Tool`` protocol attributes plus an async
``execute``; registering it makes it available to every planner mode without
touching the Agent loop.

    from examples.extensions.timestamp_tool import TimestampTool
    from synapse.modules.tools.registry import DefaultToolRegistry

    synapse = Synapse(provider="openai", model="gpt-5.4")
    synapse._container.resolve(DefaultToolRegistry).register(TimestampTool())
    result = await synapse.run("what time is it? use the timestamp tool")

The full extension walkthrough is in docs/开发/扩展指南.md.
"""

from datetime import datetime, timezone

from synapse.protocols.tool import (
    RiskLevel, ToolCategory, ToolCallMetadata, ToolResult, ToolSchema,
)


class TimestampTool:
    name = "timestamp"
    description = "Get the current UTC time in ISO-8601 format."
    parameters = ToolSchema(
        name="timestamp",
        description="Return the current UTC timestamp.",
        parameters={"type": "object", "properties": {}},
    )
    requires_sandbox = False
    risk_level = RiskLevel.READ_ONLY
    category = ToolCategory.CODE_UNDERSTANDING

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return ToolResult(
            success=True,
            output=now,
            metadata=ToolCallMetadata(tool_name=self.name),
        )
