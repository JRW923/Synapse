"""EfficiencyMetrics — token usage, tool-call counts, duration, cost estimates,
and thrashing ratio.

Subscribes to EventBus events and aggregates resource-efficiency metrics.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from synapse.core.events import EventBus
from synapse.protocols.events import BaseEvent


# ---- Snapshot ---------------------------------------------------------------


@dataclass
class EfficiencySnapshot:
    """Point-in-time snapshot of efficiency metrics."""

    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cache_hit: int = 0
    token_count_source: str = "unavailable"

    tool_call_count: int = 0
    tool_success_count: int = 0
    success_rate: float = 0.0

    duration_ms: int = 0

    cost_estimate_usd: float = 0.0
    cost_is_estimate: bool = True
    input_cost_per_million_usd: float = 0.0
    output_cost_per_million_usd: float = 0.0

    thrashing_ratio: float = 0.0


# ---- Collector --------------------------------------------------------------


class EfficiencyMetrics:
    """Collects resource-efficiency metrics from EventBus events.

    Subscribes to:
    - ``agent_completed``     — token totals, overall duration
    - ``tool_call_started``   — tool call count
    - ``tool_call_completed`` — success rate
    - ``thrashing_detected``  — thrashing ratio

    Cost estimates use a simplified pricing model:
    - Input:  ~$3.00 / 1M tokens
    - Output: ~$15.00 / 1M tokens
    (Claude-Sonnet-tier defaults; adjust for your provider.)

    Parameters
    ----------
    bus:
        The EventBus to subscribe to. If ``None``, no subscription is
        performed (useful for testing / standalone usage).
    cost_per_m_input:
        Cost in USD per 1 million input tokens.  Default: 3.00.
    cost_per_m_output:
        Cost in USD per 1 million output tokens.  Default: 15.00.
    """

    _WATCHED_EVENTS = frozenset({
        "agent_completed",
        "tool_call_started",
        "tool_call_completed",
        "thrashing_detected",
    })

    # Default input / output token split ratio (when only total_tokens is known)
    _DEFAULT_INPUT_RATIO = 0.7

    def __init__(
        self,
        bus: EventBus | None,
        cost_per_m_input: float = 3.00,
        cost_per_m_output: float = 15.00,
    ) -> None:
        self._bus = bus
        self._cost_per_m_input = cost_per_m_input
        self._cost_per_m_output = cost_per_m_output
        self.reset()

        if bus is not None:
            for event_type in self._WATCHED_EVENTS:
                bus.subscribe(event_type, self._on_event)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Reset all accumulated metrics to zero."""
        self._tokens_input = 0
        self._tokens_output = 0
        self._tokens_cache_hit = 0
        self._token_count_source = "unavailable"

        self._tool_call_count = 0
        self._tool_success_count = 0

        self._duration_ms = 0
        self._thrashing_events = 0

    def snapshot(self) -> EfficiencySnapshot:
        """Return a point-in-time snapshot of all collected efficiency metrics."""
        # Success rate
        success_rate = 0.0
        if self._tool_call_count > 0:
            success_rate = self._tool_success_count / self._tool_call_count

        # Cost estimate (USD)
        input_cost = (self._tokens_input / 1_000_000.0) * self._cost_per_m_input
        output_cost = (self._tokens_output / 1_000_000.0) * self._cost_per_m_output
        cost_estimate = round(input_cost + output_cost, 6)

        # Thrashing ratio
        thrashing_ratio = 0.0
        if self._tool_call_count > 0:
            thrashing_ratio = round(
                self._thrashing_events / self._tool_call_count, 4,
            )

        return EfficiencySnapshot(
            tokens_input=self._tokens_input,
            tokens_output=self._tokens_output,
            tokens_cache_hit=self._tokens_cache_hit,
            token_count_source=self._token_count_source,

            tool_call_count=self._tool_call_count,
            tool_success_count=self._tool_success_count,
            success_rate=round(success_rate, 4),

            duration_ms=self._duration_ms,

            cost_estimate_usd=cost_estimate,
            cost_is_estimate=True,
            input_cost_per_million_usd=self._cost_per_m_input,
            output_cost_per_million_usd=self._cost_per_m_output,

            thrashing_ratio=thrashing_ratio,
        )

    # ------------------------------------------------------------------
    # Event handler
    # ------------------------------------------------------------------

    async def _on_event(self, event: BaseEvent) -> None:
        """Dispatch to the appropriate handler based on event type."""
        etype = event.event_type
        etype_key = etype.value if hasattr(etype, "value") else str(etype)

        if etype_key == "agent_completed":
            self._handle_agent_completed(event)
        elif etype_key == "tool_call_started":
            self._handle_tool_call_started(event)
        elif etype_key == "tool_call_completed":
            self._handle_tool_call_completed(event)
        elif etype_key == "thrashing_detected":
            self._handle_thrashing_detected(event)

    # ------------------------------------------------------------------
    # Per-event-type handlers
    # ------------------------------------------------------------------

    def _handle_agent_completed(self, event: BaseEvent) -> None:
        """Track token totals and overall duration from agent_completed."""
        total_tokens = getattr(event, "total_tokens", 0)
        duration_ms = getattr(event, "duration_ms", 0)
        tokens_input = getattr(event, "tokens_input", None)
        tokens_output = getattr(event, "tokens_output", None)

        if tokens_input is not None and tokens_output is not None:
            self._tokens_input += int(tokens_input)
            self._tokens_output += int(tokens_output)
            source = "exact"
        else:
            # Compatibility for legacy producers that only sent total_tokens.
            estimated_input = int(total_tokens * self._DEFAULT_INPUT_RATIO)
            self._tokens_input += estimated_input
            self._tokens_output += total_tokens - estimated_input
            source = "estimated_70_30"

        if self._token_count_source == "unavailable":
            self._token_count_source = source
        elif self._token_count_source != source:
            self._token_count_source = "mixed"

        self._duration_ms = duration_ms

    def _handle_tool_call_started(self, event: BaseEvent) -> None:
        """Count every tool call started."""
        self._tool_call_count += 1

    def _handle_tool_call_completed(self, event: BaseEvent) -> None:
        """Count successful tool completions."""
        success = getattr(event, "success", False)
        if success:
            self._tool_success_count += 1

    def _handle_thrashing_detected(self, event: BaseEvent) -> None:
        """Count thrashing events for the thrashing ratio."""
        self._thrashing_events += 1
