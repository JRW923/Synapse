"""Context Compactor — summarization-based compression of low-priority blocks.

Phase 0 (E): truncation with provenance preservation — derived_from records
the original block id, source is preserved (not overwritten to MEMORY).
Phase 1 (E): LLMCompactor subclass in llm_compactor.py adds LLM summarization.
"""

from synapse.protocols.retriever import (
    Context,
    ContextBlock,
    ContextBudget,
)
from synapse.core.tokenizer import count_tokens


# Maximum characters to keep from the start of an OVERFLOW block before truncation.
TRUNCATION_LIMIT = 500
TRUNCATION_SUFFIX = "...[truncated]"


class ContextCompactor:
    """Compresses OVERFLOW blocks to fit within budget constraints.

    Default strategy: truncate OVERFLOW block content to the first
    TRUNCATION_LIMIT characters and append a truncation marker. The
    resulting block preserves the original `source` and records the
    original block's id in `derived_from`. All other zones pass through.
    """

    def compact(self, context: Context, budget: ContextBudget) -> Context:
        """Compress OVERFLOW blocks via truncation; preserve all other zones.

        Args:
            context: The source Context to compact.
            budget: The token budget (unused by truncation strategy).

        Returns:
            A new Context with OVERFLOW blocks truncated. SYSTEM, CORE,
            and REFERENCE zones are passed through unchanged.
        """
        return Context(
            system=list(context.system),
            core=list(context.core),
            reference=list(context.reference),
            overflow=[self._compact_block(b) for b in context.overflow],
        )

    @staticmethod
    def _compact_block(block: ContextBlock) -> ContextBlock:
        """Truncate a single OVERFLOW block, preserving provenance.

        If the block's content exceeds TRUNCATION_LIMIT characters, it is
        truncated to the first TRUNCATION_LIMIT characters and the suffix
        is appended. `source` is preserved; `derived_from` records the
        original block id.
        """
        content = block.content
        if len(content) > TRUNCATION_LIMIT:
            content = content[:TRUNCATION_LIMIT] + TRUNCATION_SUFFIX

        return ContextBlock(
            content=content,
            source=block.source,                    # preserve provenance
            priority=block.priority,
            token_count=count_tokens(content),
            derived_from=block.id,                  # link to original
            expires_after_phase=block.expires_after_phase,
            trust_annotation=block.trust_annotation,
        )
