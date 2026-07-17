"""Context Compactor — summarization-based compression of low-priority blocks.

Phase 2: simple truncation of OVERFLOW blocks (no LLM summarization yet).
Full LLM summarization comes in Phase 3.
"""

from synapse.protocols.retriever import (
    Context,
    ContextBlock,
    ContextBudget,
    ContextSource,
)


# Maximum characters to keep from the start of an OVERFLOW block before truncation.
TRUNCATION_LIMIT = 500
TRUNCATION_SUFFIX = "...[truncated]"


class ContextCompactor:
    """Compresses OVERFLOW blocks to fit within budget constraints.

    Phase 2 strategy: truncate OVERFLOW block content to the first 500
    characters and append a truncation marker. Compressed blocks are
    re-tagged with source=MEMORY. All other zones pass through unchanged.
    """

    def compact(self, context: Context, budget: ContextBudget) -> Context:
        """Compress OVERFLOW blocks via truncation; preserve all other zones.

        Args:
            context: The source Context to compact.
            budget: The token budget (unused in Phase 2 — full LLM
                    summarization in Phase 3 will use it).

        Returns:
            A new Context with OVERFLOW blocks truncated and re-tagged.
            SYSTEM, CORE, and REFERENCE zones are passed through unchanged.
        """
        return Context(
            system=list(context.system),
            core=list(context.core),
            reference=list(context.reference),
            overflow=[self._compact_block(b) for b in context.overflow],
        )

    @staticmethod
    def _compact_block(block: ContextBlock) -> ContextBlock:
        """Truncate a single OVERFLOW block and re-tag as MEMORY.

        If the block's content exceeds TRUNCATION_LIMIT characters, it is
        truncated to the first TRUNCATION_LIMIT characters and the suffix
        is appended. The source is always updated to ContextSource.MEMORY.
        """
        content = block.content
        if len(content) > TRUNCATION_LIMIT:
            content = content[:TRUNCATION_LIMIT] + TRUNCATION_SUFFIX

        return ContextBlock(
            content=content,
            source=ContextSource.MEMORY,
            priority=block.priority,
            # Rough token estimate for truncated content
            token_count=len(content) // 4,
        )
