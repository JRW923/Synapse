"""Manifest-only plugin discovery with explicit API compatibility checks."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from synapse.protocols.plugin import PluginManifest

_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$")


class DefaultPluginRegistry:
    """Discover metadata without importing arbitrary plugin code."""

    supported_api_version = "1"

    def __init__(self) -> None:
        self._plugins: dict[str, PluginManifest] = {}

    def discover(self, paths: list[str]) -> list[PluginManifest]:
        found: list[PluginManifest] = []
        for raw in paths:
            path = Path(raw)
            manifest_path = path / "synapse-plugin.yaml" if path.is_dir() else path
            data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            manifest = PluginManifest(
                name=str(data.get("name", "")),
                version=str(data.get("version", "")),
                api_version=str(data.get("api_version", "1")),
                capabilities=tuple(data.get("capabilities") or ()),
                entry_point=str(data.get("entry_point", "")),
            )
            self._validate(manifest, manifest_path)
            if manifest.name in self._plugins:
                raise ValueError(f"Duplicate plugin name '{manifest.name}'")
            self._plugins[manifest.name] = manifest
            found.append(manifest)
        return found

    def list_all(self) -> list[PluginManifest]:
        return list(self._plugins.values())

    def _validate(self, manifest: PluginManifest, source: Path) -> None:
        if not manifest.name:
            raise ValueError(f"Plugin manifest '{source}' is missing name")
        if not _SEMVER.fullmatch(manifest.version):
            raise ValueError(f"Plugin '{manifest.name}' has invalid semantic version")
        if manifest.api_version != self.supported_api_version:
            raise ValueError(
                f"Plugin '{manifest.name}' requires API {manifest.api_version}; "
                f"host supports {self.supported_api_version}"
            )
