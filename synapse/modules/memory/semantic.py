"""Semantic memory — vector-based storage backed by ChromaDB.

Uses ChromaDB with an embedding function to perform semantic similarity
search.  By default the embedding function is Chroma's built-in
``DefaultEmbeddingFunction`` (all-MiniLM-L6-v2 via sentence-transformers).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import chromadb
from chromadb.utils import embedding_functions

from synapse.protocols.memory import MemoryEntry, MemoryLevel, MemoryMetadata


class SemanticMemory:
    """Vector-based memory store for the SEMANTIC level.

    Stores entries in a ChromaDB collection named ``synapse_memory``.
    Only responds to ``MemoryLevel.SEMANTIC`` queries; all other levels
    return an empty list.

    Parameters
    ----------
    persist_dir:
        Path for persistent storage.  If ``None`` (default), an in-memory
        (ephemeral) client is used — data survives only for the lifetime of
        the process.
    embedding_function:
        Callable that maps a list of texts to a list of embedding vectors.
        Defaults to ChromaDB's built-in ``DefaultEmbeddingFunction``.
    """

    COLLECTION_NAME = "synapse_memory"

    def __init__(
        self,
        persist_dir: str | None = None,
        embedding_function: Any = None,
        collection_name: str | None = None,
    ):
        if embedding_function is None:
            embedding_function = embedding_functions.DefaultEmbeddingFunction()

        if persist_dir is not None:
            self._client = chromadb.PersistentClient(path=persist_dir)
        else:
            self._client = chromadb.EphemeralClient()

        name = collection_name if collection_name is not None else self.COLLECTION_NAME
        self._collection = self._client.get_or_create_collection(
            name=name,
            embedding_function=embedding_function,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def store(self, entry: MemoryEntry) -> None:
        """Embed *entry* content and upsert it into the collection."""
        if entry.level != MemoryLevel.SEMANTIC:
            return

        meta = _serialise_metadata(entry)
        self._collection.upsert(
            ids=[entry.id],
            documents=[entry.content],
            metadatas=[meta],
        )

    async def retrieve(
        self, query: str, level: MemoryLevel, top_k: int = 5
    ) -> list[MemoryEntry]:
        """Return the *top_k* entries most semantically similar to *query*."""
        if level != MemoryLevel.SEMANTIC:
            return []
        if self._collection.count() == 0:
            return []

        results = self._collection.query(
            query_texts=[query],
            n_results=min(top_k, self._collection.count()),
            include=["documents", "metadatas", "distances"],
        )

        return _deserialise_results(results)

    async def forget(self, entry_id: str) -> None:
        """Delete the entry identified by *entry_id* from the collection."""
        # ChromaDB's delete is a no-op if the ID is not present, so we do
        # not need to check existence first.
        self._collection.delete(ids=[entry_id])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _serialise_metadata(entry: MemoryEntry) -> dict[str, str | int | float]:
    """Convert a MemoryEntry's metadata into a flat dict suitable for
    ChromaDB's ``metadatas`` field."""
    return {
        "level": entry.level.value,
        "timestamp": entry.metadata.timestamp.isoformat(),
        "priority": entry.metadata.priority,
        "tags": ",".join(entry.metadata.tags),
        "project": entry.metadata.project or "",
        "source_task": entry.metadata.source_task or "",
        "access_count": entry.metadata.access_count,
    }


def _deserialise_results(results: dict) -> list[MemoryEntry]:
    """Convert a ChromaDB query result dict into a list of MemoryEntry."""
    entries: list[MemoryEntry] = []

    ids_batch = results.get("ids", [[]])[0]
    docs_batch = results.get("documents", [[]])[0]
    metas_batch = results.get("metadatas", [[]])[0]

    for i, entry_id in enumerate(ids_batch):
        meta: dict[str, Any] = metas_batch[i] if i < len(metas_batch) else {}
        content = docs_batch[i] if i < len(docs_batch) else ""

        # Parse stored metadata back into a MemoryMetadata.
        timestamp = datetime.now()
        ts_str = meta.get("timestamp", "")
        if ts_str:
            try:
                timestamp = datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                pass

        tags_raw: str = meta.get("tags", "")
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

        metadata = MemoryMetadata(
            project=meta.get("project") or None,
            timestamp=timestamp,
            tags=tags,
            priority=int(meta.get("priority", 5)),
            source_task=meta.get("source_task") or None,
            access_count=int(meta.get("access_count", 0)),
        )

        entries.append(
            MemoryEntry(
                id=entry_id,
                content=content,
                level=MemoryLevel.SEMANTIC,
                metadata=metadata,
            )
        )

    return entries
