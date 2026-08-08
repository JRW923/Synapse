"""LLM-driven Context Compactor — uses an LLM to summarize OVERFLOW blocks.

Phase 1 (E): when the OVERFLOW zone exceeds the configured threshold,
each block is sent to the LLM with a summarization prompt. The LLM
output replaces the block content while preserving provenance
(`source`, `derived_from`). Failures fall back to truncation.
"""

import hashlib
from synapse.protocols.retriever import (
    Context,
    ContextBlock,
    ContextBudget,
)
from synapse.protocols.llm import Message
from synapse.core.tokenizer import count_tokens


# Hard cap on input chars sent to the LLM per block — prevents huge prompts.
MAX_INPUT_CHARS = 8000
# Soft cap on summary length.
MAX_SUMMARY_CHARS = 1500

_SUMMARY_PROMPT = (
    "Summarize the following context for a coding agent. Preserve:\n"
    "- File paths and line numbers\n"
    "- Function/class/symbol names\n"
    "- Key findings, decisions, or facts\n"
    "- Any concrete values (configs, error messages, signatures)\n"
    "Drop prose and narrative. Output a dense reference, not a paragraph.\n\n"
    "--- BEGIN CONTEXT ---\n"
    "{content}\n"
    "--- END CONTEXT ---"
)


class LLMCompactor:
    """Compactor that delegates summarization to an LLM.

    Args:
        llm: An LLMProvider instance.
        fallback: A ContextCompactor to use on LLM failure (typically
            a TruncationCompactor).
    """

    def __init__(self, llm, fallback=None):
        self._llm = llm
        self._fallback = fallback
        # In-process cache keyed by content hash — avoids re-summarizing
        # identical blocks across iterations in the same task.
        self._cache: dict[str, str] = {}

    async def compact(self, context: Context, budget: ContextBudget) -> Context:
        """Compact OVERFLOW blocks via LLM summarization; preserve other zones."""
        if not context.overflow:
            return Context(
                system=list(context.system),
                core=list(context.core),
                reference=list(context.reference),
                overflow=[],
            )

        compacted_overflow: list[ContextBlock] = []
        for block in context.overflow:
            summary = await self._summarize_block(block)
            compacted_overflow.append(self._make_compact_block(block, summary))

        return Context(
            system=list(context.system),
            core=list(context.core),
            reference=list(context.reference),
            overflow=compacted_overflow,
        )

    async def _summarize_block(self, block: ContextBlock) -> str:
        """Return LLM summary for a block, with cache + fallback."""
        content = block.content[:MAX_INPUT_CHARS]
        key = hashlib.sha1(content.encode("utf-8", errors="ignore")).hexdigest()
        if key in self._cache:
            return self._cache[key]

        prompt = _SUMMARY_PROMPT.format(content=content)
        messages = [
            Message(role="system", content="You are a concise context summarizer."),
            Message(role="user", content=prompt),
        ]
        try:
            response = await self._llm.chat(messages, tools=None)
            summary = response.content.strip()[:MAX_SUMMARY_CHARS]
            if not summary:
                # Empty response — fall back.
                summary = self._fallback_summary(content)
        except Exception:
            summary = self._fallback_summary(content)

        self._cache[key] = summary
        return summary

    def _fallback_summary(self, content: str) -> str:
        """Truncation fallback when LLM fails or is unavailable."""
        from synapse.modules.context.compactor import TRUNCATION_LIMIT, TRUNCATION_SUFFIX
        if len(content) > TRUNCATION_LIMIT:
            return content[:TRUNCATION_LIMIT] + TRUNCATION_SUFFIX
        return content

    @staticmethod
    def _make_compact_block(block: ContextBlock, summary: str) -> ContextBlock:
        """Build a new block from the summary, preserving provenance."""
        return ContextBlock(
            content=summary,
            source=block.source,                # preserve original source
            priority=block.priority,
            token_count=count_tokens(summary),
            derived_from=block.id,             # link to original
            expires_after_phase=block.expires_after_phase,
            trust_annotation=block.trust_annotation,
        )
