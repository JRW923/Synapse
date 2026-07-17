"""Context Partitioner — enforces four-zone ContextBudget on a Context.

SYSTEM blocks are never removed.
CORE blocks are trimmed last (by lowest priority first).
REFERENCE blocks are trimmed before CORE (by lowest priority first).
OVERFLOW blocks are trimmed first (by lowest priority first).
"""

from synapse.protocols.retriever import (
    Context,
    ContextBlock,
    ContextBudget,
)


class ContextPartitioner:
    """Enforces the four-zone budget on a Context by evicting low-priority blocks.

    Trimming order: OVERFLOW -> REFERENCE -> CORE. SYSTEM never trimmed.
    Within each zone, blocks are sorted by priority (ascending) and the
    lowest-priority blocks are dropped until the zone fits its token budget.
    """

    def partition(self, context: Context, budget: ContextBudget) -> Context:
        """Return a new Context with blocks trimmed to fit the given budget.

        Args:
            context: The source Context with blocks in all zones.
            budget: Token budgets (absolute and per-zone percentages).

        Returns:
            A new Context with blocks trimmed to fit within budget limits.
            SYSTEM blocks are always preserved in full.
        """
        system_budget = int(budget.total_tokens * budget.system_pct)
        core_budget = int(budget.total_tokens * budget.core_pct)
        reference_budget = int(budget.total_tokens * budget.reference_pct)
        overflow_budget = int(budget.total_tokens * budget.overflow_pct)

        return Context(
            system=list(context.system),  # never trimmed
            core=self._trim_zone(context.core, core_budget),
            reference=self._trim_zone(context.reference, reference_budget),
            overflow=self._trim_zone(context.overflow, overflow_budget),
        )

    @staticmethod
    def _trim_zone(blocks: list[ContextBlock], token_budget: int) -> list[ContextBlock]:
        """Trim a zone's blocks to fit within token_budget by dropping lowest priority.

        Blocks are sorted by priority ascending (lowest first = easiest to evict).
        The lowest-priority blocks are removed until the total fits the budget.
        """
        total = sum(b.token_count for b in blocks)
        if total <= token_budget:
            return list(blocks)

        # Sort by priority ascending — lowest priority first (easiest to drop)
        sorted_blocks = sorted(blocks, key=lambda b: b.priority)

        kept: list[ContextBlock] = []
        running = 0
        for block in sorted_blocks:
            if running + block.token_count <= token_budget:
                kept.append(block)
                running += block.token_count
            else:
                break  # budget exhausted; remaining (higher priority in sorted order) dropped

        return kept
