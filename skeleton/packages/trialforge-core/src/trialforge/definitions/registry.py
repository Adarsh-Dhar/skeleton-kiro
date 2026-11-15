"""Explicit, controlled extension registry.

Third parties register *callables* under names; experiment specifications refer
to them by name. Nothing is imported by string and no source text is executed.
Each kind of extension has its own namespace so names cannot collide across kinds.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from trialforge.errors import PluginError

KINDS = ("metric", "transformation", "objective", "constraint", "optimizer", "report")


class PluginRegistry:
    """Registry of named extension callables.

    Contracts (all synchronous, pure functions):

    * ``metric(outcomes: dict[str, float]) -> float`` - derived metric from run outcomes.
    * ``transformation(values: list[float], **params) -> list[float]`` - sample transform.
    * ``objective(outcomes: dict[str, float]) -> float`` - custom objective value.
    * ``constraint(configuration: dict, outcomes: dict[str, float]) -> bool`` - feasibility.
    * ``optimizer(context: OptimizerContext) -> list[Recommendation]`` - search strategy.
    * ``report(report: ExperimentReport, result: ExperimentResult) -> dict`` - extra report section.
    """

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Callable[..., Any]]] = {k: {} for k in KINDS}

    def register(self, kind: str, name: str, fn: Callable[..., Any], *, replace: bool = False) -> None:
        """Register ``fn`` as ``name`` of ``kind``. Refuses silent overwrite."""
        if kind not in self._items:
            raise PluginError(f"unknown plugin kind '{kind}'; expected one of {KINDS}")
        if not name or not isinstance(name, str):
            raise PluginError("plugin name must be a non-empty string")
        if not callable(fn):
            raise PluginError(f"plugin '{name}' must be callable")
        if name in self._items[kind] and not replace:
            raise PluginError(f"{kind} plugin '{name}' is already registered")
        self._items[kind][name] = fn

    def get(self, kind: str, name: str) -> Callable[..., Any]:
        """Return the callable registered under (``kind``, ``name``); raises PluginError if absent."""
        try:
            return self._items[kind][name]
        except KeyError as exc:
            raise PluginError(f"no {kind} plugin registered as '{name}'") from exc

    def has(self, kind: str, name: str) -> bool:
        """True if (``kind``, ``name``) is registered."""
        return name in self._items.get(kind, {})

    def names(self, kind: str) -> list[str]:
        """Sorted names registered for ``kind``."""
        return sorted(self._items[kind])

    def unregister(self, kind: str, name: str) -> None:
        """Remove a registration (no error if absent)."""
        self._items[kind].pop(name, None)

    # convenience decorators -------------------------------------------------
    def metric(self, name: str):
        """Decorator registering a derived-metric function."""

        def deco(fn):
            self.register("metric", name, fn)
            return fn

        return deco

    def transformation(self, name: str):
        """Decorator registering a sample transformation."""

        def deco(fn):
            self.register("transformation", name, fn)
            return fn

        return deco

    def objective(self, name: str):
        """Decorator registering a custom objective function."""

        def deco(fn):
            self.register("objective", name, fn)
            return fn

        return deco

    def constraint(self, name: str):
        """Decorator registering a custom constraint function."""

        def deco(fn):
            self.register("constraint", name, fn)
            return fn

        return deco

    def optimizer(self, name: str):
        """Decorator registering an optimisation strategy."""

        def deco(fn):
            self.register("optimizer", name, fn)
            return fn

        return deco

    def report(self, name: str):
        """Decorator registering a report extension."""

        def deco(fn):
            self.register("report", name, fn)
            return fn

        return deco


DEFAULT_PLUGINS = PluginRegistry()
"""Process-wide registry used when a component is not given its own registry."""
