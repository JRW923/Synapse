"""Memory Protocol."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol


class MemoryLevel(str, Enum):
    SESSION = "session"
    PROJECT = "project"
    USER = "user"
    SEMANTIC = "semantic"


@dataclass
class MemoryMetadata:
    project: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)
    tags: list[str] = field(default_factory=list)
    priority: int = 5
    source_task: str | None = None
    access_count: int = 0


@dataclass
class MemoryEntry:
    id: str
    content: str
    level: MemoryLevel
    metadata: MemoryMetadata = field(default_factory=MemoryMetadata)


class MemoryStore(Protocol):
    """Unified interface for all memory layers."""

    async def store(self, entry: MemoryEntry) -> None: ...
    async def retrieve(self, query: str, level: MemoryLevel, top_k: int = 5) -> list[MemoryEntry]: ...
    async def forget(self, entry_id: str) -> None: ...
