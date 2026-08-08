"""Lightweight EventBus for cross-cutting concerns."""

import asyncio
import logging
import uuid
from collections import defaultdict
from collections.abc import Callable, Awaitable
from synapse.protocols.events import BaseEvent

logger = logging.getLogger(__name__)

Handler = Callable[[BaseEvent], Awaitable[None]]


class EventBus:
    """In-process pub/sub for agent events.

    Handlers are async callables. Exceptions in one handler never
    prevent other handlers from firing. Order of handler execution
    is registration order (not guaranteed across async boundaries).
    """

    def __init__(self):
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._run_id = ""
        self._trace_id = ""
        self._parent_event_id = ""

    def configure_run(self, run_id: str | None = None, trace_id: str | None = None) -> str:
        """Start a correlation context for events emitted by the current run."""
        self._run_id = run_id or str(uuid.uuid4())
        self._trace_id = trace_id or self._run_id
        self._parent_event_id = ""
        return self._run_id

    def subscribe(self, event_type: str, handler: Handler) -> None:
        """Register an async handler for an event type."""
        self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Handler) -> None:
        """Remove a previously registered handler."""
        try:
            self._handlers[event_type].remove(handler)
        except (KeyError, ValueError):
            pass

    def has_subscribers(self, event_type: str) -> bool:
        """True if any handler is registered for *event_type*.

        Used by the planner to detect whether the CLI is already rendering
        progress (rich live panel), so it can stay silent on stderr instead of
        interleaving raw lines with the panel.
        """
        return bool(self._handlers.get(event_type))

    async def emit(self, event: BaseEvent) -> None:
        """Fire an event to all registered handlers.

        Handlers run concurrently. Exceptions are logged, never raised.
        """
        if not getattr(event, "run_id", ""):
            if not self._run_id:
                self.configure_run()
            if hasattr(event, "run_id"):
                event.run_id = self._run_id
        if hasattr(event, "trace_id") and not event.trace_id:
            event.trace_id = self._trace_id or getattr(event, "run_id", "")
        if hasattr(event, "parent_event_id") and not event.parent_event_id:
            event.parent_event_id = self._parent_event_id
        self._parent_event_id = getattr(event, "event_id", self._parent_event_id)

        handlers = self._handlers.get(event.event_type, [])
        if not handlers:
            return

        results = await asyncio.gather(
            *[self._safe_invoke(h, event) for h in handlers],
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.warning("EventBus handler error: %s", result)

    async def _safe_invoke(self, handler: Handler, event: BaseEvent) -> None:
        await handler(event)
