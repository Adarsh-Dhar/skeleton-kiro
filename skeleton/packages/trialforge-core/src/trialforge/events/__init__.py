"""Minimal synchronous event bus for lifecycle notifications."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("trialforge.events")


@dataclass(frozen=True)
class Event:
    """A lifecycle event, e.g. ``run.completed``."""

    name: str
    experiment_id: str
    run_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


class EventBus:
    """Subscribers never break the engine: handler exceptions are logged and swallowed."""

    def __init__(self) -> None:
        self._handlers: list[tuple[str, Callable[[Event], None]]] = []

    def subscribe(self, prefix: str, handler: Callable[[Event], None]) -> None:
        """Subscribe ``handler`` to events whose name starts with ``prefix`` ('' = all)."""
        self._handlers.append((prefix, handler))

    def publish(self, event: Event) -> None:
        """Deliver ``event`` to matching subscribers; handler exceptions are logged and never propagate."""
        for prefix, handler in self._handlers:
            if event.name.startswith(prefix):
                try:
                    handler(event)
                except Exception:  # noqa: BLE001 - observers must not affect execution
                    log.exception("event handler failed for %s", event.name)


__all__ = ["Event", "EventBus"]
