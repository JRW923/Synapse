"""Evaluation-only switches for isolating Harness module effects."""

from __future__ import annotations

from synapse.core.evaluation import EvaluationAblations


class DisabledMemoryStore:
    """MemoryStore-compatible sink used by the memory ablation."""

    async def store(self, _entry) -> None:
        return None

    async def retrieve(self, _query, _level, top_k: int = 5) -> list:
        return []

    async def forget(self, _entry_id: str) -> None:
        return None

    def clear_session(self) -> None:
        return None
