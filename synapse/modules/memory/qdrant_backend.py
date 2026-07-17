"""Semantic memory — vector-based storage backed by Qdrant (local mode).

Uses qdrant-client in local/path-based mode — no server required.
Embeddings are computed via a built-in deterministic TF-IDF-style hashing
function by default, with optional sentence-transformers support.

Usage as an alternative to ChromaDB SemanticMemory::

    from synapse import Synapse

    agent = Synapse(provider="anthropic", memory_backend="qdrant")
    result = await agent.run("Fix the bug in auth.py")
"""

from __future__ import annotations

import hashlib
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    PointIdsList,
    PointStruct,
    VectorParams,
)

from synapse.protocols.memory import MemoryEntry, MemoryLevel, MemoryMetadata


# ---------------------------------------------------------------------------
# Built-in deterministic embedding (no external dependencies)
# ---------------------------------------------------------------------------


class TfidfEmbedding:
    """Deterministic embedding function using hash-based word vectors.

    Each word is mapped to a pseudo-random vector via SHA-256, and the
    document embedding is the mean of its word vectors.  Stop words are
    filtered out.  This produces meaningful (though basic) semantic
    similarity without requiring sentence-transformers or any model
    downloads.

    Parameters
    ----------
    dim:
        Dimensionality of the embedding vectors.  Default 384 (matches
        all-MiniLM-L6-v2, the sentence-transformers default).
    """

    def __init__(self, dim: int = 384):
        self.dim = dim

    def __call__(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def embed_query(self, query: str) -> list[float]:
        """Embed a single query string (convenience wrapper)."""
        return self._embed_one(query)

    def embed_documents(self, documents: list[str]) -> list[list[float]]:
        """Embed a batch of document strings (convenience wrapper)."""
        return self.__call__(documents)

    # -- internal ----------------------------------------------------------

    def _embed_one(self, text: str) -> list[float]:
        """Produce a single embedding vector for *text*."""
        words = self._tokenize(text)
        if not words:
            return [0.0] * self.dim

        acc = [0.0] * self.dim
        for w in words:
            wv = self._word_vector(w, self.dim)
            for i in range(self.dim):
                acc[i] += wv[i]
        n = len(words)
        return [v / n for v in acc]

    @classmethod
    def _tokenize(cls, text: str) -> list[str]:
        """Lowercase, split on non-alphanumeric, drop short / stop tokens."""
        _STOP = frozenset({
            "the", "is", "are", "was", "were", "be", "been", "being",
            "have", "has", "had", "do", "does", "did", "will", "would",
            "can", "could", "shall", "should", "may", "might", "must",
            "a", "an", "and", "or", "but", "if", "then", "else", "when",
            "where", "why", "how", "all", "each", "every", "both", "few",
            "more", "most", "other", "some", "such", "no", "not", "only",
            "own", "same", "so", "than", "too", "very", "just", "in",
            "on", "at", "to", "of", "for", "from", "by", "as", "into",
            "with", "about", "it", "its", "this", "that", "these", "those",
            "we", "you", "he", "she", "they", "me", "him", "her", "us",
            "them", "my", "your", "his", "our", "their",
        })
        tokens = re.split(r"[^a-zA-Z0-9]+", text.lower())
        return [t for t in tokens if len(t) >= 2 and t not in _STOP]

    @classmethod
    def _word_vector(cls, word: str, dim: int = 384) -> list[float]:
        """Deterministic pseudo-random vector for a single word."""
        h = hashlib.sha256(word.encode()).digest()
        vec: list[float] = []
        for i in range(dim):
            b = h[i % len(h)]
            vec.append((b / 127.5) - 1.0)
        return vec


# ---------------------------------------------------------------------------
# Sentence-transformers embedding (optional, used when available)
# ---------------------------------------------------------------------------


def _try_load_sentence_transformer(model_name: str = "all-MiniLM-L6-v2"):
    """Attempt to load a sentence-transformer model.

    Returns the model instance if ``sentence_transformers`` is installed,
    otherwise returns ``None``.
    """
    try:
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(model_name)
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# QdrantMemory
# ---------------------------------------------------------------------------


class QdrantMemory:
    """Vector-based memory store for the SEMANTIC level backed by Qdrant.

    Stores entries in a Qdrant collection named ``synapse_memory``.
    Only responds to ``MemoryLevel.SEMANTIC`` queries; all other levels
    return an empty list.

    Qdrant runs in local/path-based mode — no server or Docker required.
    Data is stored on disk at *persist_dir*.

    Parameters
    ----------
    persist_dir:
        Path for persistent storage.  If ``None`` (default), a temporary
        directory is used — data survives only for the lifetime of the
        process.
    embedding_function:
        Callable that maps a list of texts to a list of embedding vectors.
        Defaults to the built-in :class:`TfidfEmbedding` (no external
        dependencies).  For stronger semantic matching, use a
        sentence-transformer model (e.g. ``all-MiniLM-L6-v2``).
    collection_name:
        Name of the Qdrant collection.  Defaults to ``"synapse_memory"``.
    vector_dim:
        Dimensionality of embedding vectors.  Must match the output of
        *embedding_function*.  Defaults to 384.
    """

    COLLECTION_NAME = "synapse_memory"

    def __init__(
        self,
        persist_dir: str | None = None,
        embedding_function: Any = None,
        collection_name: str | None = None,
        vector_dim: int = 384,
    ):
        # -- embedding function --
        if embedding_function is None:
            # Try sentence-transformers first, fall back to built-in TfidfEmbedding
            st_model = _try_load_sentence_transformer()
            if st_model is not None:
                embedding_function = st_model
            else:
                embedding_function = TfidfEmbedding(dim=vector_dim)

        self._embed = embedding_function
        self._vector_dim = vector_dim

        # -- Qdrant client (local mode) --
        if persist_dir is not None:
            path = Path(persist_dir)
            path.mkdir(parents=True, exist_ok=True)
            self._client = QdrantClient(path=str(path))
            self._temp_dir = None
        else:
            self._temp_dir = tempfile.TemporaryDirectory(prefix="synapse_qdrant_")
            self._client = QdrantClient(path=self._temp_dir.name)

        # -- collection --
        name = collection_name if collection_name is not None else self.COLLECTION_NAME
        self._collection_name = name
        self._ensure_collection()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def store(self, entry: MemoryEntry) -> None:
        """Embed *entry* content and upsert it into the Qdrant collection."""
        if entry.level != MemoryLevel.SEMANTIC:
            return

        # Compute embedding for the content
        vector = self._embed_text(entry.content)

        # Serialise metadata into payload
        meta = _serialise_metadata(entry)

        point_id = _str_to_uuid(entry.id)

        point = PointStruct(
            id=point_id,
            vector=vector,
            payload={
                "synapse_id": entry.id,
                "content": entry.content,
                **meta,
            },
        )

        self._client.upsert(
            collection_name=self._collection_name,
            points=[point],
        )

    async def retrieve(
        self, query: str, level: MemoryLevel, top_k: int = 5
    ) -> list[MemoryEntry]:
        """Return the *top_k* entries most semantically similar to *query*."""
        if level != MemoryLevel.SEMANTIC:
            return []

        # Check if collection has any points
        if self._client.count(collection_name=self._collection_name).count == 0:
            return []

        # Compute query embedding
        query_vector = self._embed_text(query)

        results = self._client.query_points(
            collection_name=self._collection_name,
            query=query_vector,
            limit=min(top_k, 100),
            with_payload=True,
            with_vectors=False,
        )

        return _deserialise_results(results.points)

    async def forget(self, entry_id: str) -> None:
        """Delete the entry identified by *entry_id* from the collection."""
        point_id = _str_to_uuid(entry_id)
        try:
            self._client.delete(
                collection_name=self._collection_name,
                points_selector=PointIdsList(ids=[point_id]),
            )
        except Exception:
            # Qdrant may raise if the ID does not exist; treat as no-op.
            pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_collection(self) -> None:
        """Create the collection if it does not already exist."""
        existing = self._client.get_collections()
        names = {c.name for c in existing.collections}
        if self._collection_name not in names:
            self._client.create_collection(
                collection_name=self._collection_name,
                vectors_config=VectorParams(
                    size=self._vector_dim,
                    distance=Distance.COSINE,
                ),
            )

    def _embed_text(self, text: str) -> list[float]:
        """Compute an embedding vector for a single *text* string.

        Handles both sentence-transformer models (``.encode()``) and the
        built-in TfidfEmbedding callable.
        """
        # sentence-transformers: model.encode(text) returns a list of floats
        if hasattr(self._embed, "encode"):
            result = self._embed.encode([text])
            # May return numpy array or list; normalise to list[float]
            return _to_float_list(result[0])

        # TfidfEmbedding / callable: __call__(list[str]) -> list[list[float]]
        if callable(self._embed):
            results = self._embed([text])
            return _to_float_list(results[0])

        raise TypeError(
            f"Unsupported embedding function type: {type(self._embed)}"
        )

    def close(self) -> None:
        """Release resources and clean up temporary storage."""
        try:
            self._client.close()
        except Exception:
            pass
        if self._temp_dir is not None:
            try:
                self._temp_dir.cleanup()
            except Exception:
                pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# UUID v5 namespace for deterministic conversion of string IDs
_QDRANT_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def _str_to_uuid(entry_id: str) -> str:
    """Convert a string ID to a deterministic UUID for Qdrant.

    Uses UUID v5 with a fixed namespace so the same string always maps to
    the same UUID, enabling idempotent upserts and reliable deletes.
    """
    return str(uuid.uuid5(_QDRANT_NAMESPACE, entry_id))


def _to_float_list(vec: Any) -> list[float]:
    """Convert a vector (list, numpy array, etc.) to a ``list[float]``."""
    # numpy array
    if hasattr(vec, "tolist"):
        result = vec.tolist()
        return [float(x) for x in result]
    # already a list/tuple
    return [float(x) for x in vec]


def _serialise_metadata(entry: MemoryEntry) -> dict[str, str | int | float]:
    """Convert a MemoryEntry's metadata into a flat dict suitable for
    Qdrant's payload field."""
    return {
        "level": entry.level.value,
        "timestamp": entry.metadata.timestamp.isoformat(),
        "priority": entry.metadata.priority,
        "tags": ",".join(entry.metadata.tags),
        "project": entry.metadata.project or "",
        "source_task": entry.metadata.source_task or "",
        "access_count": entry.metadata.access_count,
    }


def _deserialise_results(results: list) -> list[MemoryEntry]:
    """Convert a Qdrant search result list into a list of MemoryEntry.

    *results* is expected to be a list of ``ScoredPoint`` objects from
    ``QueryResponse.points``, each with ``.id``, ``.payload``, and
    ``.score``.
    """
    entries: list[MemoryEntry] = []

    for hit in results:
        # hit.payload holds the payload we stored during upsert
        payload: dict[str, Any] = getattr(hit, "payload", None) or {}
        content = payload.get("content", "")
        # Recover the original synapse ID from the payload
        synapse_id = payload.get("synapse_id", str(hit.id))

        # Parse stored metadata back into a MemoryMetadata.
        timestamp = datetime.now()
        ts_str = payload.get("timestamp", "")
        if ts_str:
            try:
                timestamp = datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                pass

        tags_raw: str = payload.get("tags", "")
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

        metadata = MemoryMetadata(
            project=payload.get("project") or None,
            timestamp=timestamp,
            tags=tags,
            priority=int(payload.get("priority", 5)),
            source_task=payload.get("source_task") or None,
            access_count=int(payload.get("access_count", 0)),
        )

        entries.append(
            MemoryEntry(
                id=synapse_id,
                content=content,
                level=MemoryLevel.SEMANTIC,
                metadata=metadata,
            )
        )

    return entries
