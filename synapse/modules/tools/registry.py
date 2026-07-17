"""Default in-memory tool registry."""

from synapse.protocols.tool import Tool, ToolCategory


class DefaultToolRegistry:
    """Mutable registry of tools, queryable by name or category."""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Remove a tool by name. No-op if not registered."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"Tool '{name}' not registered")
        return self._tools[name]

    def list_all(self) -> list[Tool]:
        return list(self._tools.values())

    def list_by_category(self, category: str) -> list[Tool]:
        cat = ToolCategory(category) if isinstance(category, str) else category
        return [t for t in self._tools.values() if t.category == cat]

    def get_schemas(self) -> list[dict]:
        """Return schemas in Anthropic-compatible format."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters.parameters,
            }
            for t in self._tools.values()
        ]
