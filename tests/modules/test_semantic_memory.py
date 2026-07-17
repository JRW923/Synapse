"""Tests for semantic memory backed by ChromaDB."""
import pytest
from synapse.protocols.memory import MemoryEntry, MemoryLevel, MemoryMetadata


def _make_entry(id_: str, content: str) -> MemoryEntry:
    return MemoryEntry(
        id=id_,
        content=content,
        level=MemoryLevel.SEMANTIC,
        metadata=MemoryMetadata(tags=["test"]),
    )


# ---------------------------------------------------------------------------
# Deterministic embedding function for reproducible tests
# ---------------------------------------------------------------------------

class _DeterministicEmbedding:
    """Bag-of-words embedding with a fixed, seeded vocabulary.

    Each word is mapped to a deterministic pseudo-random vector via SHA-256.
    The document embedding is the mean of its word vectors.  This gives
    meaningful (though weak) semantic similarity without downloading a model.
    """

    DIM = 384

    def __call__(self, input: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in input:
            vec = self._embed_one(text)
            vectors.append(vec)
        return vectors

    def embed_query(self, input: list[str]) -> list[list[float]]:
        """ChromaDB calls this for query embeddings."""
        return self.__call__(input)

    # -- word-level helpers ---------------------------------------------------

    @classmethod
    def _embed_one(cls, text: str) -> list[float]:
        """Produce a single embedding vector for *text*."""
        words = cls._tokenize(text)
        if not words:
            return [0.0] * cls.DIM

        acc = [0.0] * cls.DIM
        for w in words:
            wv = cls._word_vector(w)
            for i in range(cls.DIM):
                acc[i] += wv[i]
        n = len(words)
        return [v / n for v in acc]

    @classmethod
    def _tokenize(cls, text: str) -> list[str]:
        """Lowercase, split on non-alphanumeric, drop short / stop tokens."""
        import re

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
    def _word_vector(cls, word: str) -> list[float]:
        """Deterministic pseudo-random vector for a single word."""
        import hashlib

        h = hashlib.sha256(word.encode()).digest()
        vec: list[float] = []
        for i in range(cls.DIM):
            b = h[i % len(h)]
            vec.append((b / 127.5) - 1.0)
        return vec

    # -- ChromaDB protocol surface --------------------------------------------

    @staticmethod
    def name() -> str:
        return "deterministic-test"

    @staticmethod
    def build_from_config(config: dict) -> "_DeterministicEmbedding":
        return _DeterministicEmbedding()

    def get_config(self) -> dict:
        return {"name": self.name()}

    def is_legacy(self) -> bool:
        return False

    def default_space(self) -> str:
        return "cosine"

    def supported_spaces(self) -> list[str]:
        return ["cosine", "l2", "ip"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

import uuid


def _unique_name() -> str:
    """Return a unique collection name so tests do not interfere with
    each other when ChromaDB shares an in-memory backend."""
    return f"test_semantic_{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_store_retrieve():
    """An entry stored at SEMANTIC level can be retrieved by a matching
    query."""
    from synapse.modules.memory.semantic import SemanticMemory

    mem = SemanticMemory(embedding_function=_DeterministicEmbedding(), collection_name=_unique_name())
    entry = _make_entry("e1", "The quick brown fox jumps over the lazy dog")
    await mem.store(entry)

    results = await mem.retrieve("quick brown fox", MemoryLevel.SEMANTIC, top_k=5)
    assert len(results) == 1
    assert results[0].id == "e1"
    assert results[0].content == entry.content


@pytest.mark.asyncio
async def test_similarity_ranking():
    """More relevant entries should rank higher in retrieval results."""
    from synapse.modules.memory.semantic import SemanticMemory

    mem = SemanticMemory(embedding_function=_DeterministicEmbedding(), collection_name=_unique_name())

    await mem.store(_make_entry("python", "Python is a popular programming language used for web development, data science, and AI."))
    await mem.store(_make_entry("coffee", "Coffee is a brewed drink prepared from roasted coffee beans."))
    await mem.store(_make_entry("javascript", "JavaScript is a scripting language that enables interactive web pages."))

    # Query about programming — python and javascript should rank above coffee.
    results = await mem.retrieve("programming language for building software", MemoryLevel.SEMANTIC, top_k=3)
    assert len(results) == 3
    # The top result should be the most programming-related entry.
    top_ids = [r.id for r in results]
    assert top_ids[0] in ("python", "javascript")
    # coffee should be last (least relevant).
    assert top_ids[-1] == "coffee"


@pytest.mark.asyncio
async def test_wrong_level_empty():
    """Non-SEMANTIC retrieval levels return an empty list even when
    semantic entries exist."""
    from synapse.modules.memory.semantic import SemanticMemory

    mem = SemanticMemory(embedding_function=_DeterministicEmbedding(), collection_name=_unique_name())
    entry = _make_entry("e1", "semantic content")
    await mem.store(entry)

    for level in (MemoryLevel.SESSION, MemoryLevel.PROJECT, MemoryLevel.USER):
        results = await mem.retrieve("semantic", level, top_k=5)
        assert results == [], f"Expected empty list for {level}"
