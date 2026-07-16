"""Session memory — in-memory only, released when session ends."""

from synapse.protocols.memory import MemoryEntry, MemoryLevel


class SessionMemory:
    """In-memory storage for the SESSION level only.

    Session memory is ephemeral — cleared when the process exits.
    For Phase 1, retrieval is simple substring matching.
    """

    def __init__(self):
        self._entries: dict[str, MemoryEntry] = {}

    async def store(self, entry: MemoryEntry) -> None:
        if entry.level == MemoryLevel.SESSION:
            self._entries[entry.id] = entry

    async def retrieve(self, query: str, level: MemoryLevel, top_k: int = 5) -> list[MemoryEntry]:
        if level != MemoryLevel.SESSION:
            return []

        matches = []
        for entry in self._entries.values():
            if query.lower() in entry.content.lower():
                matches.append(entry)
            elif any(query.lower() in tag.lower() for tag in entry.metadata.tags):
                matches.append(entry)

        matches.sort(key=lambda e: e.metadata.priority, reverse=True)
        return matches[:top_k]

    async def forget(self, entry_id: str) -> None:
        self._entries.pop(entry_id, None)
