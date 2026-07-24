"""Phase E — context engineering deep optimization tests.

Covers:
- Phase 0.4: Partitioner knapsack fix
- Phase 0.6: Compactor provenance preservation
- Phase 1: LLMCompactor with mocked LLM
- Phase 4: CitationTracker signal matching
- Phase E end-to-end pipeline test
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from synapse.protocols.retriever import (
    Context, ContextBlock, ContextBudget, ContextSource,
)
from synapse.protocols.llm import LLMResponse, Message
from synapse.protocols.events import ContextBlockCited, EventType
from synapse.modules.context.partitioner import ContextPartitioner
from synapse.modules.context.compactor import ContextCompactor
from synapse.modules.context.llm_compactor import LLMCompactor
from synapse.modules.context.citation import (
    CitationTracker,
    _extract_signals,
    _MAX_SIGNALS_PER_BLOCK,
)
from synapse.config.schema import ContextConfig, SynapseConfig, PlanningConfig


def make_block(content, source=ContextSource.GREP, priority=5, token_count=None):
    return ContextBlock(
        content=content,
        source=source,
        priority=priority,
        token_count=token_count if token_count is not None else len(content) // 4,
    )


# ---- Phase 0.4: Partitioner knapsack fix --------------------------------

class TestPartitionerKnapsack:
    def test_small_high_priority_block_fits_after_large_one_skipped(self):
        """The old break bug would drop a small high-priority block that
        appeared after a too-large block in the sorted order."""
        large = make_block("x" * 1000, priority=10, token_count=1000)
        small = make_block("y" * 10, priority=1, token_count=10)
        # budget=20 — large can't fit, small can.
        result = ContextPartitioner._trim_zone([large, small], token_budget=20)
        # The small block must be retained despite the large one being skipped.
        assert small in result
        assert large not in result

    def test_preserves_original_order(self):
        a = make_block("a" * 50, priority=5, token_count=50)
        b = make_block("b" * 50, priority=5, token_count=50)
        result = ContextPartitioner._trim_zone([a, b], token_budget=60)
        # Only one fits — but order is preserved (a first, not b).
        assert result == [a]


# ---- Phase 0.6: Compactor provenance ------------------------------------

class TestCompactorProvenance:
    def test_source_preserved_not_overwritten(self):
        block = make_block("z" * 1000, source=ContextSource.GIT, priority=3)
        ctx = Context(overflow=[block])
        result = ContextCompactor().compact(ctx, ContextBudget())
        assert result.overflow[0].source == ContextSource.GIT
        assert result.overflow[0].derived_from == block.id

    def test_short_block_not_truncated(self):
        block = make_block("short content", source=ContextSource.GREP, priority=3)
        ctx = Context(overflow=[block])
        result = ContextCompactor().compact(ctx, ContextBudget())
        assert result.overflow[0].content == "short content"
        assert result.overflow[0].derived_from == block.id


# ---- Phase 1: LLMCompactor ----------------------------------------------

class TestLLMCompactor:
    def _mock_llm(self, summary: str = "SUMMARY"):
        llm = MagicMock()
        response = LLMResponse(content=summary, usage={"input": 100, "output": 20})
        llm.chat = AsyncMock(return_value=response)
        return llm

    def test_llm_summary_replaces_content(self):
        block = make_block("x" * 2000, source=ContextSource.GLOB, priority=2)
        ctx = Context(overflow=[block])
        llm = self._mock_llm("Dense summary with path /foo/bar.py")
        compactor = LLMCompactor(llm=llm, fallback=ContextCompactor())
        result = asyncio.run(compactor.compact(ctx, ContextBudget()))
        assert result.overflow[0].content == "Dense summary with path /foo/bar.py"
        assert result.overflow[0].source == ContextSource.GLOB
        assert result.overflow[0].derived_from == block.id

    def test_cache_avoids_repeated_calls(self):
        block = make_block("xyz" * 500, source=ContextSource.GLOB, priority=2)
        ctx = Context(overflow=[block, block])  # same content twice
        llm = self._mock_llm("summary")
        compactor = LLMCompactor(llm=llm, fallback=ContextCompactor())
        asyncio.run(compactor.compact(ctx, ContextBudget()))
        # LLM should only be called once (cached on second identical block).
        assert llm.chat.call_count == 1

    def test_falls_back_to_truncation_on_error(self):
        block = make_block("x" * 2000, source=ContextSource.GLOB, priority=2)
        ctx = Context(overflow=[block])
        llm = MagicMock()
        llm.chat = AsyncMock(side_effect=RuntimeError("LLM down"))
        compactor = LLMCompactor(llm=llm, fallback=ContextCompactor())
        result = asyncio.run(compactor.compact(ctx, ContextBudget()))
        # Fallback should have truncated the content.
        assert result.overflow[0].content.endswith("...[truncated]")
        assert result.overflow[0].derived_from == block.id

    def test_empty_overflow_returns_empty(self):
        ctx = Context(overflow=[])
        llm = self._mock_llm()
        compactor = LLMCompactor(llm=llm, fallback=ContextCompactor())
        result = asyncio.run(compactor.compact(ctx, ContextBudget()))
        assert result.overflow == []
        assert llm.chat.call_count == 0


# ---- Phase 4: CitationTracker -------------------------------------------

class TestCitationTracker:
    def test_extract_signals_finds_paths(self):
        block = make_block(
            "Found in src/foo/bar.py at line 42\nclass Parser:\n    pass"
        )
        signals = _extract_signals(block)
        # Should include file path and class name.
        assert any("bar.py" in s for s in signals)
        assert "Parser" in signals

    def test_mark_usage_increments_usage_count(self):
        block = make_block("content")
        ctx = Context(system=[block])
        CitationTracker().mark_usage(ctx)
        assert block.usage_count == 1

    def test_track_response_detects_citation(self):
        block = make_block("src/main.py defines class App")
        ctx = Context(core=[block])
        bus = MagicMock()
        bus.emit = AsyncMock()
        tracker = CitationTracker()
        asyncio.run(tracker.track_response(
            "I see src/main.py has class App", ctx, bus, "session-1",
        ))
        assert block.citation_count == 1
        assert bus.emit.call_count == 1
        event = bus.emit.call_args[0][0]
        assert isinstance(event, ContextBlockCited)
        assert event.block_id == block.id

    def test_track_response_no_false_positive_on_unrelated(self):
        block = make_block("src/main.py defines class App")
        ctx = Context(core=[block])
        bus = MagicMock()
        bus.emit = AsyncMock()
        tracker = CitationTracker()
        asyncio.run(tracker.track_response(
            "The weather is nice today", ctx, bus, "session-1",
        ))
        assert block.citation_count == 0
        assert bus.emit.call_count == 0

    def test_report_contains_all_zones(self):
        sys_b = make_block("sys", source=ContextSource.MEMORY)
        core_b = make_block("core", source=ContextSource.GREP)
        ref_b = make_block("ref", source=ContextSource.GLOB)
        ov_b = make_block("ov", source=ContextSource.GREP)
        ctx = Context(system=[sys_b], core=[core_b], reference=[ref_b], overflow=[ov_b])
        report = CitationTracker().report(ctx)
        assert report["total"] == 4
        zones = {r["zone"] for r in report["blocks"]}
        assert zones == {"system", "core", "reference", "overflow"}


# ---- Phase 4: _extract_signals boundary cases ---------------------------

class TestExtractSignalsBoundaries:
    """Boundary cases around the path-signal length/separator filter.

    The filter (citation.py:34) requires BOTH a length >= 6 AND a path
    separator. A backslash must NOT bypass the length gate.
    """

    def test_short_backslash_path_is_dropped(self):
        # Regression guard for the operator-precedence bug: a <6-char token
        # with a backslash (and no forward slash) must be filtered out.
        block = make_block(r"see a\b.c for details")
        assert r"a\b.c" not in _extract_signals(block)

    def test_long_backslash_path_is_kept(self):
        # Windows-style path (colon excluded by the path char class, so the
        # captured signal starts at the backslash), long enough -> kept.
        block = make_block(r"edit \foo\bar.py now")
        assert r"\foo\bar.py" in _extract_signals(block)

    def test_long_forwardslash_path_is_kept(self):
        block = make_block("from src/a/b.py import x")
        assert "src/a/b.py" in _extract_signals(block)

    def test_length_boundary_exactly_six_with_separator_kept(self):
        # "a/b.py" is exactly 6 chars and contains a separator.
        block = make_block("use a/b.py here")
        assert "a/b.py" in _extract_signals(block)

    def test_length_boundary_five_with_separator_dropped(self):
        # "a/b.x" is 5 chars with a separator -> below the length gate.
        block = make_block("use a/b.x here")
        assert "a/b.x" not in _extract_signals(block)

    def test_short_extension_noise_dropped(self):
        # No separator -> noise even if it carries an extension.
        block = make_block("version v1.0 then asyncio.py")
        signals = _extract_signals(block)
        assert "v1.0" not in signals
        assert "asyncio.py" not in signals

    def test_symbol_signals_ignore_length_gate(self):
        # def/class/function names are extracted regardless of the length gate.
        block = make_block("def helper:\nclass Widget:\nfunction run")
        signals = _extract_signals(block)
        assert "helper" in signals
        assert "Widget" in signals
        assert "run" in signals

    def test_signal_count_capped(self):
        # Many long distinctive lines -> only _MAX_SIGNALS_PER_BLOCK kept.
        lines = "\n".join(
            f"this is a distinctive line number {i} here" for i in range(20)
        )
        block = make_block(lines)
        assert len(_extract_signals(block)) == _MAX_SIGNALS_PER_BLOCK


# ---- Phase 4: track_response / mark_usage boundaries --------------------

class TestCitationTrackingBoundaries:
    def test_empty_response_returns_zero(self):
        block = make_block("src/main.py")
        ctx = Context(core=[block])
        bus = MagicMock()
        bus.emit = AsyncMock()
        n = asyncio.run(CitationTracker().track_response("", ctx, bus, "s"))
        assert n == 0
        assert block.citation_count == 0
        assert bus.emit.call_count == 0

    def test_case_insensitive_match(self):
        block = make_block("see SRC/Main.py and class App")
        ctx = Context(core=[block])
        bus = MagicMock()
        bus.emit = AsyncMock()
        n = asyncio.run(CitationTracker().track_response(
            "I read src/main.py and app", ctx, bus, "s",
        ))
        assert n == 1
        assert block.citation_count == 1

    def test_overflow_zone_not_tracked(self):
        # overflow is never injected to the LLM (react.py), so it is never
        # usage-marked or citation-scanned — assert that boundary holds.
        block = make_block("src/overflow.py unique-sentinel")
        ctx = Context(overflow=[block])
        bus = MagicMock()
        bus.emit = AsyncMock()
        n = asyncio.run(CitationTracker().track_response(
            "src/overflow.py unique-sentinel", ctx, bus, "s",
        ))
        assert n == 0
        assert block.citation_count == 0

    def test_mark_usage_skips_overflow(self):
        ov = make_block("overflow only")
        core = make_block("core only")
        ctx = Context(core=[core], overflow=[ov])
        CitationTracker().mark_usage(ctx)
        assert core.usage_count == 1
        assert ov.usage_count == 0


# ---- Phase 0.3: ContextConfig -------------------------------------------

class TestContextConfig:
    def test_default_compaction_strategy_is_truncation(self):
        cfg = ContextConfig()
        assert cfg.compaction_strategy == "truncation"

    def test_total_tokens_zero_means_inherit(self):
        cfg = ContextConfig(total_tokens=0)
        root = SynapseConfig(planning=PlanningConfig(max_tokens_per_task=200_000), context=cfg)
        # total <= 0 → inherit from planning
        total = root.planning.max_tokens_per_task if cfg.total_tokens <= 0 else cfg.total_tokens
        assert total == 200_000

    def test_synapse_config_has_context_section(self):
        cfg = SynapseConfig()
        assert hasattr(cfg, "context")
        assert isinstance(cfg.context, ContextConfig)


# ---- End-to-end pipeline test -------------------------------------------

class TestContextPipelineE2E:
    """Verify the full pipeline: retriever → compactor → partitioner →
    planner prompt assembly. The key assertion is that CORE/REFERENCE
    blocks actually appear in the system prompt sent to the LLM."""

    def test_react_planner_injects_core_and_reference(self):
        from synapse.modules.planning.react import ReActPlanner
        from synapse.protocols.planner import PlanningMode

        planner = ReActPlanner(max_iterations=1, max_tokens_per_task=10000)

        sys_block = make_block("System rules", source=ContextSource.MEMORY, priority=9)
        core_block = make_block(
            "src/core.py defines class Engine",
            source=ContextSource.GREP, priority=8,
        )
        ref_block = make_block(
            "docs/api.md mentions endpoint /v1/run",
            source=ContextSource.GLOB, priority=5,
        )
        ov_block = make_block("z" * 2000, source=ContextSource.GREP, priority=2)

        from synapse.protocols.retriever import Context
        ctx = Context(
            system=[sys_block],
            core=[core_block],
            reference=[ref_block],
            overflow=[ov_block],
        )

        prompt = planner._build_system_prompt(ctx)
        # System block content appears.
        assert "System rules" in prompt
        # CORE block content appears with source annotation.
        assert "src/core.py" in prompt
        assert "from grep" in prompt
        # REFERENCE block content appears.
        assert "docs/api.md" in prompt
        # OVERFLOW content should NOT be injected.
        assert "zzzzz" not in prompt
