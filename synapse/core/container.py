"""Lightweight IoC container for Synapse."""

from collections.abc import Callable
from typing import Any, get_origin


class Container:
    """Simple dependency injection container.

    Registers implementations against Protocol types. Supports
    singleton instances (register) and factory functions (register_factory).
    """

    def __init__(self):
        self._instances: dict[type, Any] = {}
        self._factories: dict[type, Callable[[], Any]] = {}

    def register(self, proto_type: type, instance: object) -> None:
        """Register a singleton instance for a protocol type."""
        key = self._normalize_type(proto_type)
        self._instances[key] = instance
        self._factories.pop(key, None)

    def register_factory(self, proto_type: type, factory: Callable[[], object]) -> None:
        """Register a factory that creates a new instance each resolve."""
        key = self._normalize_type(proto_type)
        self._factories[key] = factory
        self._instances.pop(key, None)

    def resolve(self, proto_type: type) -> object:
        """Resolve a protocol type to its registered implementation."""
        key = self._normalize_type(proto_type)

        if key in self._factories:
            return self._factories[key]()

        if key in self._instances:
            return self._instances[key]

        # Try parent types / generic origins
        if (origin := get_origin(proto_type)) and origin in self._instances:
            return self._instances[origin]
        if origin and origin in self._factories:
            return self._factories[origin]()

        raise KeyError(f"No implementation registered for {proto_type.__name__}")

    @staticmethod
    def _normalize_type(t: type) -> type:
        """Strip generic parameters to get the base type."""
        origin = get_origin(t)
        return origin if origin is not None else t
