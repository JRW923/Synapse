"""Versioned plugin manifest contracts."""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class PluginManifest:
    name: str
    version: str
    api_version: str = "1"
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    entry_point: str = ""


class PluginRegistry(Protocol):
    def discover(self, paths: list[str]) -> list[PluginManifest]: ...
    def list_all(self) -> list[PluginManifest]: ...
