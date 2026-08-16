"""Memory composition used by the public adapter layer."""

from __future__ import annotations

from synapse.modules.memory.project import ProjectMemory
from synapse.modules.memory.session import SessionMemory
from synapse.modules.memory.user import UserMemory
from synapse.protocols.memory import MemoryEntry, MemoryLevel


class LayeredMemory:
    """Route memory operations to the store for each lifecycle level."""

    def __init__(
        self,
        session_memory: SessionMemory,
        project_memory: ProjectMemory,
        user_memory: UserMemory,
        semantic_memory=None,
    ) -> None:
        self._session = session_memory
        self._project = project_memory
        self._user = user_memory
        # A factory keeps optional vector backends lazy until first use.
        self._semantic = semantic_memory

    def _get_semantic(self):
        if callable(self._semantic):
            try:
                self._semantic = self._semantic()
            except Exception:
                self._semantic = None
        return self._semantic

    async def store(self, entry: MemoryEntry) -> None:
        stores = {
            MemoryLevel.SESSION: self._session,
            MemoryLevel.PROJECT: self._project,
            MemoryLevel.USER: self._user,
        }
        if entry.level in stores:
            await stores[entry.level].store(entry)
        elif entry.level == MemoryLevel.SEMANTIC:
            semantic = self._get_semantic()
            if semantic is not None:
                await semantic.store(entry)

    async def retrieve(
        self, query: str, level: MemoryLevel, top_k: int = 5
    ) -> list[MemoryEntry]:
        stores = {
            MemoryLevel.SESSION: self._session,
            MemoryLevel.PROJECT: self._project,
            MemoryLevel.USER: self._user,
        }
        if level in stores:
            return await stores[level].retrieve(query, level, top_k)
        if level == MemoryLevel.SEMANTIC:
            semantic = self._get_semantic()
            if semantic is not None:
                return await semantic.retrieve(query, level, top_k)
        return []

    async def forget(self, entry_id: str) -> None:
        await self._session.forget(entry_id)
        await self._project.forget(entry_id)
        await self._user.forget(entry_id)
        if self._semantic is not None and not callable(self._semantic):
            await self._semantic.forget(entry_id)

    def clear_session(self) -> None:
        self._session.clear()

    def close(self) -> None:
        """Release a materialized semantic backend, if it owns resources."""
        semantic = self._semantic
        if semantic is None or callable(semantic):
            return
        close = getattr(semantic, "close", None)
        if close is not None:
            close()

    def __del__(self) -> None:
        # Best-effort fallback for short-lived API instances that were not
        # explicitly closed by their caller.
        try:
            self.close()
        except Exception:
            pass
