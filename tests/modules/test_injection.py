"""Tests for InjectionGuard — prompt injection defense via trust annotation."""

import pytest

from synapse.protocols.retriever import (
    Context,
    ContextBlock,
    ContextSource,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_block(content: str, source: ContextSource) -> ContextBlock:
    """Create a ContextBlock with a simple token estimate."""
    return ContextBlock(
        content=content,
        source=source,
        priority=5,
        token_count=len(content) // 4,
    )


# ---------------------------------------------------------------------------
# Test 1: system blocks are classified as TrustLevel.SYSTEM
# ---------------------------------------------------------------------------

def test_system_blocks_tagged_system():
    """Blocks placed in context.system must be annotated as TrustLevel.SYSTEM."""
    from synapse.modules.security.injection import InjectionGuard, TrustLevel

    ctx = Context(
        system=[
            make_block("# CLAUDE.md project instructions", ContextSource.MEMORY),
            make_block("# Security rules", ContextSource.MEMORY),
        ],
        core=[
            make_block("def foo(): pass", ContextSource.GREP),
        ],
    )

    guard = InjectionGuard()
    annotated = guard.annotate(ctx)

    # Every block in system should be SYSTEM
    for block in annotated.system:
        assert block.trust_annotation is not None
        assert block.trust_annotation.level == TrustLevel.SYSTEM

    # The core block should NOT be SYSTEM
    for block in annotated.core:
        assert block.trust_annotation is not None
        assert block.trust_annotation.level != TrustLevel.SYSTEM


# ---------------------------------------------------------------------------
# Test 2: web content is classified as TrustLevel.EXTERNAL
# ---------------------------------------------------------------------------

def test_web_content_tagged_external():
    """Blocks from ContextSource.WEB must be annotated as TrustLevel.EXTERNAL."""
    from synapse.modules.security.injection import InjectionGuard, TrustLevel

    ctx = Context(
        core=[
            make_block("<html>malicious prompt injection</html>", ContextSource.WEB),
        ],
        reference=[
            make_block('{"data": "api response"}', ContextSource.API),
            make_block("SELECT * FROM users", ContextSource.DB),
        ],
    )

    guard = InjectionGuard()
    annotated = guard.annotate(ctx)

    # WEB source → EXTERNAL
    assert annotated.core[0].trust_annotation is not None
    assert annotated.core[0].trust_annotation.level == TrustLevel.EXTERNAL

    # API source → EXTERNAL
    assert annotated.reference[0].trust_annotation is not None
    assert annotated.reference[0].trust_annotation.level == TrustLevel.EXTERNAL

    # DB source → EXTERNAL
    assert annotated.reference[1].trust_annotation is not None
    assert annotated.reference[1].trust_annotation.level == TrustLevel.EXTERNAL


# ---------------------------------------------------------------------------
# Test 3: wrap_for_llm adds correct XML tags for EXTERNAL blocks
# ---------------------------------------------------------------------------

def test_wrap_for_llm_adds_external_tags():
    """wrap_for_llm must wrap EXTERNAL blocks in <external-content> tags."""
    from synapse.modules.security.injection import InjectionGuard, TrustLevel, TrustAnnotation

    guard = InjectionGuard()

    # Manually annotate a block as EXTERNAL
    block = make_block("unsafe web content", ContextSource.WEB)
    block.trust_annotation = TrustAnnotation(
        level=TrustLevel.EXTERNAL,
        reason="Fetched from web",
    )

    wrapped = guard.wrap_for_llm(block)

    # Must contain the opening and closing tags
    assert '<external-content source="web">' in wrapped
    assert "</external-content>" in wrapped
    assert "unsafe web content" in wrapped

    # Non-EXTERNAL blocks should NOT be wrapped in tags
    system_block = make_block("CLAUDE.md instructions", ContextSource.MEMORY)
    system_block.trust_annotation = TrustAnnotation(
        level=TrustLevel.SYSTEM,
        reason="System instructions",
    )
    unwrapped = guard.wrap_for_llm(system_block)
    assert "<external-content" not in unwrapped
    assert "CLAUDE.md instructions" == unwrapped
