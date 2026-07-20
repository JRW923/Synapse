"""Phase 2 + 3 tests — citation rate display, task classifier,
budget profiles, and historical feedback."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from synapse.protocols.retriever import (
    Context, ContextBlock, ContextBudget, ContextSource,
)
from synapse.modules.context.classifier import classify_task, TaskType
from synapse.modules.context.budget import (
    TASK_BUDGET_PROFILES, select_budget, BudgetHistory,
    _MIN_SAMPLES_FOR_ADJUSTMENT, _MAX_SHIFT_PCT,
)


def make_block(content, source=ContextSource.GREP, priority=5):
    return ContextBlock(
        content=content,
        source=source,
        priority=priority,
        token_count=len(content) // 4,
    )


# ---- Phase 3.1: Task classifier ----------------------------------------

class TestTaskClassifier:
    def test_debug_wins_over_test(self):
        """'fix failing test' should be DEBUG, not TEST."""
        assert classify_task("fix the failing test") == TaskType.DEBUG

    def test_pure_test(self):
        assert classify_task("add a unit test for parser") == TaskType.TEST

    def test_refactor(self):
        assert classify_task("refactor the auth module") == TaskType.REFACTOR

    def test_feature(self):
        assert classify_task("add user login") == TaskType.FEATURE

    def test_doc(self):
        assert classify_task("write README") == TaskType.DOC
        assert classify_task("update documentation") == TaskType.DOC

    def test_chinese_keywords(self):
        assert classify_task("修复登录bug") == TaskType.DEBUG
        assert classify_task("新增用户模块") == TaskType.FEATURE
        assert classify_task("重构认证逻辑") == TaskType.REFACTOR

    def test_unknown(self):
        assert classify_task("tell me a joke") == TaskType.UNKNOWN

    def test_empty(self):
        assert classify_task("") == TaskType.UNKNOWN

    def test_case_insensitive(self):
        assert classify_task("FIX THE BUG") == TaskType.DEBUG
        assert classify_task("Refactor The Module") == TaskType.REFACTOR


# ---- Phase 3.2: Budget profiles ----------------------------------------

class TestBudgetProfiles:
    def test_all_task_types_have_profiles(self):
        for tt in TaskType:
            assert tt in TASK_BUDGET_PROFILES, f"missing profile for {tt}"

    def test_test_profile_favors_reference(self):
        b = select_budget(TaskType.TEST, 100_000)
        # TEST: reference_pct should be at least 0.40 (large reference need).
        assert b.reference_pct >= 0.40

    def test_refactor_profile_favors_core(self):
        b = select_budget(TaskType.REFACTOR, 100_000)
        assert b.core_pct >= 0.60

    def test_debug_profile_favors_reference(self):
        b = select_budget(TaskType.DEBUG, 100_000)
        assert b.reference_pct >= 0.50

    def test_total_tokens_inherited(self):
        b = select_budget(TaskType.UNKNOWN, 250_000)
        assert b.total_tokens == 250_000

    def test_unknown_falls_back_to_feature(self):
        b_unk = select_budget(TaskType.UNKNOWN, 100_000)
        b_feat = select_budget(TaskType.FEATURE, 100_000)
        assert b_unk.system_pct == b_feat.system_pct
        assert b_unk.core_pct == b_feat.core_pct


# ---- Phase 3.3: BudgetHistory -------------------------------------------

class TestBudgetHistory:
    def test_cold_start_returns_base_unchanged(self):
        """Below _MIN_SAMPLES_FOR_ADJUSTMENT, no adjustment applied."""
        bh = BudgetHistory()
        base = select_budget(TaskType.TEST, 100_000)
        result = bh.suggest_adjustment(TaskType.TEST, base)
        assert result.system_pct == base.system_pct
        assert result.core_pct == base.core_pct

    @pytest.mark.asyncio
    async def test_adjustment_after_enough_samples(self):
        """After N samples, budget should shift toward high-citation zones."""
        bh = BudgetHistory()
        # Simulate TEST tasks where reference zone is heavily cited.
        for _ in range(_MIN_SAMPLES_FOR_ADJUSTMENT):
            report = {
                "blocks": [
                    {"zone": "system", "cited": 1, "usage": 2},
                    {"zone": "core", "cited": 0, "usage": 3},
                    {"zone": "reference", "cited": 5, "usage": 5},  # 100% cited
                    {"zone": "overflow", "cited": 0, "usage": 1},
                ],
                "total": 4,
            }
            await bh.record(TaskType.TEST, report)

        base = select_budget(TaskType.TEST, 100_000)
        adjusted = bh.suggest_adjustment(TaskType.TEST, base)
        # Reference should increase (or stay) — never decrease.
        assert adjusted.reference_pct >= base.reference_pct
        # And the shift should be capped.
        assert (adjusted.reference_pct - base.reference_pct) <= _MAX_SHIFT_PCT + 0.001

    @pytest.mark.asyncio
    async def test_record_handles_none_report(self):
        bh = BudgetHistory()
        await bh.record(TaskType.FEATURE, None)  # should not raise
        assert bh._load(TaskType.FEATURE)["samples"] == 0

    @pytest.mark.asyncio
    async def test_record_handles_empty_blocks(self):
        bh = BudgetHistory()
        await bh.record(TaskType.FEATURE, {"blocks": [], "total": 0})
        assert bh._load(TaskType.FEATURE)["samples"] == 0


# ---- Phase 2: citation summary format ----------------------------------

class TestCitationSummary:
    def test_format_citation_summary_no_synapse(self):
        from synapse.adapters.cli import _format_citation_summary
        assert _format_citation_summary(None) == ""

    def test_format_citation_summary_no_data(self):
        from synapse.adapters.cli import _format_citation_summary
        synapse = MagicMock()
        synapse.get_citation_report.return_value = None
        assert _format_citation_summary(synapse) == ""

    def test_format_citation_summary_with_data(self):
        from synapse.adapters.cli import _format_citation_summary
        synapse = MagicMock()
        synapse.get_citation_report.return_value = {
            "blocks": [
                {"zone": "system", "cited": 2, "usage": 3},
                {"zone": "core", "cited": 1, "usage": 5},
                {"zone": "reference", "cited": 0, "usage": 2},
            ],
            "total": 3,
        }
        line = _format_citation_summary(synapse)
        assert "system 2/3 cited" in line
        assert "core 1/5 cited" in line
        assert "reference 0/2 cited" in line
