"""Tests for evaluation-only Harness ablation switches."""

import pytest

from synapse.eval.ablations import DisabledMemoryStore, EvaluationAblations
from synapse.protocols.memory import MemoryEntry, MemoryLevel


def test_ablation_mapping_defaults_and_rejects_unknown_fields() -> None:
    assert EvaluationAblations.from_value(None).to_dict() == {
        "context": True,
        "memory": True,
        "completion_gate": True,
        "action_auth": True,
    }
    assert not EvaluationAblations.from_value({"memory": False}).memory

    with pytest.raises(ValueError, match="Unknown eval ablation fields"):
        EvaluationAblations.from_value({"memroy": False})
    with pytest.raises(TypeError, match="must be boolean"):
        EvaluationAblations.from_value({"memory": 0})


@pytest.mark.asyncio
async def test_disabled_memory_store_never_retains_entries() -> None:
    store = DisabledMemoryStore()
    await store.store(MemoryEntry(id="1", content="secret", level=MemoryLevel.SESSION))
    assert await store.retrieve("secret", MemoryLevel.SESSION) == []
