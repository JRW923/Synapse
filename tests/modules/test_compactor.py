"""Tests for ContextCompactor — OVERFLOW truncation (Phase 2)."""

from synapse.protocols.retriever import (
    Context,
    ContextBlock,
    ContextBudget,
    ContextSource,
)
from synapse.modules.context.compactor import ContextCompactor


def make_block(content: str, source: ContextSource, priority: int, token_count: int) -> ContextBlock:
    return ContextBlock(
        content=content,
        source=source,
        priority=priority,
        token_count=token_count,
    )


class TestContextCompactor:
    """Unit tests for ContextCompactor.compact()."""

    def test_compactor_truncates_overflow(self):
        """OVERFLOW blocks longer than 500 chars should be truncated."""
        long_content = "x" * 1200
        ctx = Context(
            overflow=[
                make_block(long_content, ContextSource.GLOB, priority=3, token_count=300),
            ],
            core=[],
            system=[],
            reference=[],
        )

        budget = ContextBudget(total_tokens=10000)
        compactor = ContextCompactor()
        result = compactor.compact(ctx, budget)

        # Should be truncated to first 500 chars + "...[truncated]"
        assert len(result.overflow) == 1
        block = result.overflow[0]
        assert len(block.content) <= 500 + len("...[truncated]")
        assert block.content.endswith("...[truncated]")
        assert block.source == ContextSource.MEMORY

    def test_compactor_preserves_core(self):
        """CORE blocks must not be modified by compaction."""
        core_content = "important code here"
        ctx = Context(
            core=[
                make_block(core_content, ContextSource.GREP, priority=8, token_count=100),
            ],
            overflow=[
                make_block("x" * 1000, ContextSource.GLOB, priority=3, token_count=250),
            ],
            system=[],
            reference=[],
        )

        budget = ContextBudget(total_tokens=10000)
        compactor = ContextCompactor()
        result = compactor.compact(ctx, budget)

        # Core blocks should be untouched
        assert len(result.core) == 1
        assert result.core[0].content == core_content
        assert result.core[0].source == ContextSource.GREP
        assert result.core[0].token_count == 100

        # Overflow should still be compacted
        assert len(result.overflow) == 1
        assert result.overflow[0].content.endswith("...[truncated]")
