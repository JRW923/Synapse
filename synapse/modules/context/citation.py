"""Citation Tracker — detects when LLM responses reference context blocks.

Phase 4 (E): scans LLM response content for signals derived from each
ContextBlock (file paths, symbols, distinctive phrases) and emits
`ContextBlockCited` events. Also updates the block's `citation_count`
and `usage_count` fields.

Heuristic matching — not exact. Deliberately favors precision over
recall: we only count a citation when we find a strong signal, so the
counts are a lower bound.
"""

import re
from pathlib import Path
from synapse.protocols.retriever import Context, ContextBlock


# Minimum distinctive phrase length to count as a citation signal.
_MIN_PHRASE_LEN = 12
# How many distinct signals per block to test before declaring a citation.
_MAX_SIGNALS_PER_BLOCK = 5


def _extract_signals(block: ContextBlock) -> list[str]:
    """Extract distinctive substrings from a block that, if found in
    an LLM response, suggest the LLM used this block."""
    content = block.content
    signals: list[str] = []

    # 1. File paths (most reliable signal).
    for m in re.finditer(r"[\w./\\-]+\.\w{1,6}", content):
        path = m.group(0)
        # Filter out trivial noise like "v1.0" or "asyncio.py" if too short.
        if len(path) >= 6 and "/" in path or "\\" in path:
            signals.append(path)

    # 2. Def/function/class signatures.
    for m in re.finditer(r"(?:def|class|function)\s+(\w+)", content):
        signals.append(m.group(1))

    # 3. Long distinctive lines (skip whitespace-only / very short lines).
    for line in content.splitlines():
        line = line.strip()
        if len(line) >= _MIN_PHRASE_LEN and not line.startswith("#"):
            # Skip very common patterns.
            if line in ("---", "```", "```python"):
                continue
            signals.append(line)
        if len(signals) >= _MAX_SIGNALS_PER_BLOCK:
            break

    # Deduplicate, preserve order.
    seen: set[str] = set()
    out: list[str] = []
    for s in signals:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out[:_MAX_SIGNALS_PER_BLOCK]


class CitationTracker:
    """Tracks which context blocks the LLM appears to use in responses.

    Lifecycle:
    - `mark_usage(context)` — call before sending context to the LLM,
      increments `usage_count` on every block in the prompt.
    - `track_response(response_content, context, event_bus, session_id)`
      — call after each LLM response; scans content for signals from
      each block and emits ContextBlockCited events for matches.
    """

    def __init__(self):
        self._signals_cache: dict[str, list[str]] = {}

    def mark_usage(self, context: Context) -> None:
        """Increment usage_count on blocks that will be sent to the LLM."""
        for block in context.system + context.core + context.reference:
            block.usage_count += 1

    async def track_response(
        self,
        response_content: str,
        context: Context,
        event_bus,
        session_id: str,
    ) -> int:
        """Scan response for citations of context blocks; emit events.

        Returns the number of citations detected.
        """
        from synapse.protocols.events import ContextBlockCited

        if not response_content:
            return 0

        cited_count = 0
        # Lowercase once for case-insensitive substring search.
        haystack = response_content.lower()

        for block in context.system + context.core + context.reference:
            if block.id not in self._signals_cache:
                self._signals_cache[block.id] = _extract_signals(block)
            signals = self._signals_cache[block.id]
            if not signals:
                continue

            matched_signal = None
            for sig in signals:
                if sig.lower() in haystack:
                    matched_signal = sig
                    break

            if matched_signal is not None:
                block.citation_count += 1
                cited_count += 1
                snippet = response_content[:200]
                try:
                    await event_bus.emit(ContextBlockCited(
                        session_id=session_id,
                        block_id=block.id,
                        block_source=block.source.value,
                        response_snippet=snippet,
                    ))
                except Exception:
                    pass

        return cited_count

    def report(self, context: Context) -> dict:
        """Return a per-block usage/citation report for the /context-report command."""
        rows = []
        for zone in ("system", "core", "reference", "overflow"):
            blocks = getattr(context, zone, [])
            for b in blocks:
                rows.append({
                    "zone": zone,
                    "id": b.id,
                    "source": b.source.value,
                    "priority": b.priority,
                    "tokens": b.token_count,
                    "usage": b.usage_count,
                    "cited": b.citation_count,
                    "citation_rate": (
                        f"{b.citation_count}/{b.usage_count}"
                        if b.usage_count > 0 else "0/0"
                    ),
                })
        return {"blocks": rows, "total": len(rows)}
