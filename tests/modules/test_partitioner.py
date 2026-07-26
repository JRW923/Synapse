"""Tests for ContextPartitioner — four-zone budget enforcement."""

from synapse.protocols.retriever import (
    Context,
    ContextBlock,
    ContextBudget,
    ContextSource,
)
from synapse.modules.context.partitioner import ContextPartitioner


def make_block(content: str, source: ContextSource, priority: int, token_count: int) -> ContextBlock:
    return ContextBlock(
        content=content,
        source=source,
        priority=priority,
        token_count=token_count,
    )


class TestContextPartitioner:
    """Unit tests for ContextPartitioner.partition()."""

    def test_partitioner_trims_overflow_first(self):
        """OVERFLOW blocks should be the first to be evicted when budget is tight."""
        ctx = Context(
            system=[make_block("sys", ContextSource.MEMORY, priority=10, token_count=100)],
            core=[make_block("core", ContextSource.GREP, priority=8, token_count=200)],
            reference=[make_block("ref", ContextSource.MEMORY, priority=5, token_count=300)],
            overflow=[
                make_block("ov1", ContextSource.GLOB, priority=3, token_count=500),
                make_block("ov2", ContextSource.GLOB, priority=2, token_count=500),
                make_block("ov3", ContextSource.GLOB, priority=1, token_count=500),
            ],
        )

        # Total = 100 + 200 + 300 + 1500 = 2100
        # Budget: total_tokens=800 — overflow budget = 80 tokens
        # overflow has 3 blocks (1500 tokens), only 80 allowed -> trim priority-1 first
        budget = ContextBudget(total_tokens=800)
        partitioner = ContextPartitioner()
        result = partitioner.partition(ctx, budget)

        # System and core should survive
        assert len(result.system) == 1
        assert len(result.core) == 1
        # Reference may or may not survive depending on budget
        # Overflow should be trimmed aggressively
        assert len(result.overflow) <= 1  # at most one overflow block survives

        # The lowest-priority overflow block (priority=1, ov3) should be the first removed
        if len(result.overflow) == 1:
            assert result.overflow[0].priority >= 2  # priority 1 was dropped

    def test_partitioner_never_removes_system(self):
        """SYSTEM blocks must never be evicted, even if they exceed budget."""
        ctx = Context(
            system=[
                make_block("sys1", ContextSource.MEMORY, priority=10, token_count=5000),
                make_block("sys2", ContextSource.MEMORY, priority=10, token_count=5000),
            ],
            core=[make_block("core1", ContextSource.GREP, priority=8, token_count=100)],
        )

        # Total system = 10000 tokens. Budget total = 500, system_pct=0.15 => 75 tokens
        # System blocks far exceed budget but must NOT be removed
        budget = ContextBudget(total_tokens=500)
        partitioner = ContextPartitioner()
        result = partitioner.partition(ctx, budget)

        # All system blocks must be preserved, regardless of budget
        assert len(result.system) == 2
        assert result.system[0].content == "sys1"
        assert result.system[1].content == "sys2"

    def test_protects_compacted_overflow_summaries(self):
        """Compacted OVERFLOW summaries (derived_from set) must survive
        partitioning even when a higher-priority reference block wins the
        budget — otherwise the ContextCompactor's summary is lost."""
        high_ref = make_block("important reference", ContextSource.MEMORY, priority=9, token_count=400)
        # A compacted summary: tiny, low priority, but derived_from is set.
        summary = make_block("zzz", ContextSource.GREP, priority=2, token_count=120)
        summary.derived_from = "orig123"  # mark as compacted overflow

        # reference budget = 25% of 2000 = 500; high_ref(400)+summary(120)=520,
        # so only one fits. Without protection the priority-9 ref wins and the
        # summary is dropped; with protection the summary survives.
        ctx = Context(reference=[high_ref, summary])
        budget = ContextBudget(total_tokens=2000, reference_pct=0.25)
        result = ContextPartitioner().partition(ctx, budget)

        derived = [b for b in result.reference if b.derived_from]
        assert derived, "compacted overflow summary must survive partitioning"
        assert derived[0].content == "zzz"

    def test_unmarked_blocks_trim_as_before(self):
        """Blocks without derived_from still trim by priority (no regression)."""
        big = make_block("big", ContextSource.MEMORY, priority=10, token_count=1000)
        small = make_block("small", ContextSource.MEMORY, priority=1, token_count=10)
        ctx = Context(reference=[big, small])
        # budget=20 -> big can't fit, small can.
        result = ContextPartitioner().partition(ctx, ContextBudget(total_tokens=80, reference_pct=0.25))
        assert small in result.reference
        assert big not in result.reference
