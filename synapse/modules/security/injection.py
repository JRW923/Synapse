"""Prompt injection defense via trust annotation.

Implements annotation-based injection defense: classify every ContextBlock
by trust level so the LLM can judge trustworthiness itself. No content is
filtered — this is "标注而非过滤" (annotate, not filter).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from synapse.protocols.retriever import (
    Context,
    ContextBlock,
    ContextSource,
)


# ---------------------------------------------------------------------------
# Trust domain model
# ---------------------------------------------------------------------------

class TrustLevel(str, Enum):
    """Granularity of trust for a context block.

    * SYSTEM    — project CLAUDE.md, security rules (not user-modifiable)
    * USER      — direct user input, conversational messages
    * DETERMINISTIC — grep / glob / AST / git results (tool output, deterministic)
    * EXTERNAL  — web fetches, API responses, DB queries (untrusted)
    """
    SYSTEM = "system"
    USER = "user"
    DETERMINISTIC = "deterministic"
    EXTERNAL = "external"


@dataclass(frozen=True)
class TrustAnnotation:
    """Immutable trust classification attached to a ContextBlock.

    ``level`` gives the trust tier; ``reason`` is a human-readable
    explanation of the classification decision.
    """
    level: TrustLevel
    reason: str


# ---------------------------------------------------------------------------
# Source → TrustLevel mapping tables
# ---------------------------------------------------------------------------

# Tool results that are deterministic (local, reproducible)
_DETERMINISTIC_SOURCES: frozenset[ContextSource] = frozenset({
    ContextSource.GREP,
    ContextSource.GLOB,
    ContextSource.AST,
    ContextSource.GIT,
})

# Sources that fetch data from outside the trusted boundary
_EXTERNAL_SOURCES: frozenset[ContextSource] = frozenset({
    ContextSource.WEB,
    ContextSource.API,
    ContextSource.DB,
})


# ---------------------------------------------------------------------------
# InjectionGuard
# ---------------------------------------------------------------------------

class InjectionGuard:
    """Annotates every block in a Context with a TrustLevel.

    The guard does **not** filter or modify content — it only labels blocks
    so the LLM (or a downstream policy) can decide how to treat each piece.

    Usage::

        guard = InjectionGuard()
        annotated_ctx = guard.annotate(context)
        for block in annotated_ctx.all_blocks:
            prompt_part = guard.wrap_for_llm(block)
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def annotate(self, context: Context) -> Context:
        """Classify every block in *context* by TrustLevel.

        The annotation is stored directly on each ``ContextBlock`` via its
        ``trust_annotation`` field.  The same ``Context`` object is returned
        for chaining convenience.
        """
        for block in context.all_blocks:
            annotation = self._classify(block, context)
            block.trust_annotation = annotation
        return context

    def wrap_for_llm(self, block: ContextBlock) -> str:
        """Return the block content, wrapped in trust-related XML tags.

        EXTERNAL blocks are enclosed in ``<external-content source="...">``
        tags so the LLM can distinguish them from trusted material.
        All other trust levels are returned as plain content.
        """
        annotation = block.trust_annotation
        if annotation is not None and annotation.level == TrustLevel.EXTERNAL:
            source_label = block.source.value if hasattr(block.source, "value") else str(block.source)
            return (
                f'<external-content source="{source_label}">'
                f"{block.content}"
                f"</external-content>"
            )
        return block.content

    # ------------------------------------------------------------------
    # Classification logic
    # ------------------------------------------------------------------

    @staticmethod
    def _classify(block: ContextBlock, context: Context) -> TrustAnnotation:
        """Return the TrustAnnotation for a single block."""

        # 1. System blocks — always SYSTEM
        #    Blocks in context.system contain project instructions
        #    (CLAUDE.md, AGENTS.md, README.md) and are not user-modifiable.
        if block in context.system:
            return TrustAnnotation(
                level=TrustLevel.SYSTEM,
                reason="Project-level system instructions (e.g. CLAUDE.md)",
            )

        # 2. Memory-sourced blocks that are NOT in system
        #    These come from session/user/project memory stores.
        if block.source == ContextSource.MEMORY:
            return TrustAnnotation(
                level=TrustLevel.USER,
                reason="Memory-retrieved content (session/user/project memory)",
            )

        # 3. User input — always USER
        if block.source == ContextSource.USER_INPUT:
            return TrustAnnotation(
                level=TrustLevel.USER,
                reason="Direct user input",
            )

        # 4. Deterministic tool output
        if block.source in _DETERMINISTIC_SOURCES:
            return TrustAnnotation(
                level=TrustLevel.DETERMINISTIC,
                reason=f"Deterministic tool result ({block.source.value})",
            )

        # 5. External sources
        if block.source in _EXTERNAL_SOURCES:
            return TrustAnnotation(
                level=TrustLevel.EXTERNAL,
                reason=f"External data source ({block.source.value})",
            )

        # 6. Fallback — RETRIEVER or unknown
        return TrustAnnotation(
            level=TrustLevel.USER,
            reason=f"Default classification for source: {block.source.value}",
        )
