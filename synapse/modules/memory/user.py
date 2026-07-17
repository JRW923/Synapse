"""User memory — file-based persistent storage in ~/.synapse/memory/.

Stores YAML frontmatter Markdown files, one per entry. This layer provides
cross-project persistence: any project on the same machine shares the same
user memory.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml

from synapse.protocols.memory import MemoryEntry, MemoryLevel, MemoryMetadata


def _default_memory_dir() -> Path:
    """Return the default user memory directory.  Wrapped in a callable so
    tests can monkeypatch ``Path.home`` before construction."""
    return Path.home() / ".synapse" / "memory"


def _serialise_entry(entry: MemoryEntry) -> str:
    """Serialise a MemoryEntry to a YAML-frontmatter Markdown string."""
    frontmatter = {
        "id": entry.id,
        "level": entry.level.value,
        "project": entry.metadata.project,
        "timestamp": entry.metadata.timestamp.isoformat(),
        "tags": entry.metadata.tags,
        "priority": entry.metadata.priority,
        "source_task": entry.metadata.source_task,
        "access_count": entry.metadata.access_count,
    }
    yaml_block = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).rstrip("\n")
    return f"---\n{yaml_block}\n---\n\n{entry.content}\n"


def _deserialise_entry(raw: str) -> MemoryEntry | None:
    """Parse a YAML-frontmatter Markdown string back into a MemoryEntry.

    Returns None if the file cannot be parsed (e.g. missing or malformed frontmatter).
    """
    if not raw.startswith("---"):
        return None

    try:
        end_idx = raw.index("---", 3)
    except ValueError:
        return None

    yaml_block = raw[3:end_idx].strip()
    content = raw[end_idx + 3:].strip()

    try:
        frontmatter = yaml.safe_load(yaml_block)
    except yaml.YAMLError:
        return None

    if not isinstance(frontmatter, dict):
        return None

    entry_id = frontmatter.get("id", "")
    level_str = frontmatter.get("level", "user")
    try:
        level = MemoryLevel(level_str)
    except ValueError:
        level = MemoryLevel.USER

    timestamp_str = frontmatter.get("timestamp")
    try:
        timestamp = datetime.fromisoformat(timestamp_str) if timestamp_str else datetime.now()
    except (TypeError, ValueError):
        timestamp = datetime.now()

    metadata = MemoryMetadata(
        project=frontmatter.get("project"),
        timestamp=timestamp,
        tags=frontmatter.get("tags") or [],
        priority=frontmatter.get("priority", 5),
        source_task=frontmatter.get("source_task"),
        access_count=frontmatter.get("access_count", 0),
    )

    return MemoryEntry(
        id=entry_id,
        content=content,
        level=level,
        metadata=metadata,
    )


class UserMemory:
    """File-based persistent memory for the USER level.

    Stores entries as YAML-frontmatter Markdown files under
    ``~/.synapse/memory/``.  Only responds to ``MemoryLevel.USER`` queries;
    all other levels return an empty list.
    """

    def __init__(self, memory_dir: Path | None = None):
        self._dir = Path(memory_dir) if memory_dir is not None else _default_memory_dir()

    async def store(self, entry: MemoryEntry) -> None:
        if entry.level != MemoryLevel.USER:
            return

        self._dir.mkdir(parents=True, exist_ok=True)
        file_path = self._dir / f"{entry.id}.md"
        file_path.write_text(_serialise_entry(entry), encoding="utf-8")

    async def retrieve(
        self, query: str, level: MemoryLevel, top_k: int = 5
    ) -> list[MemoryEntry]:
        if level != MemoryLevel.USER:
            return []

        if not self._dir.is_dir():
            return []

        entries: list[MemoryEntry] = []
        query_lower = query.lower()

        for file_path in sorted(self._dir.glob("*.md")):
            raw = file_path.read_text(encoding="utf-8")
            parsed = _deserialise_entry(raw)
            if parsed is None:
                continue

            # Match against content or tags.
            if query_lower in parsed.content.lower():
                entries.append(parsed)
            elif any(query_lower in tag.lower() for tag in parsed.metadata.tags):
                entries.append(parsed)

        entries.sort(key=lambda e: e.metadata.priority, reverse=True)
        return entries[:top_k]

    async def forget(self, entry_id: str) -> None:
        file_path = self._dir / f"{entry_id}.md"
        try:
            file_path.unlink()
        except FileNotFoundError:
            pass
