"""Exception hierarchy for TRIALFORGE.

All SDK-raised errors derive from :class:`TrialforgeError`. ``DesignError`` and
``AdapterError`` remain importable from their historical modules.
"""

from __future__ import annotations


class TrialforgeError(Exception):
    """Base class for every error raised deliberately by the SDK."""


class ValidationError(TrialforgeError, ValueError):
    """An experiment definition, configuration or measurement failed validation."""


class DesignError(TrialforgeError, ValueError):
    """A design could not be generated (budget, incompatible method, no feasible runs)."""


class AdapterError(TrialforgeError):
    """Base class for adapter-originated execution failures."""


class AdapterUnavailableError(AdapterError):
    """The selected adapter is not registered or reports itself unavailable."""


class CapabilityError(AdapterError):
    """The adapter lacks a capability required by the requested operation."""


class ExecutionError(TrialforgeError):
    """The execution engine could not carry out an operation."""


class InvalidTransitionError(ExecutionError):
    """A lifecycle state transition is not permitted."""


class PersistenceError(TrialforgeError):
    """A repository operation failed."""


class AnalysisError(TrialforgeError):
    """An analysis could not be performed on the supplied data."""


class OptimizationError(TrialforgeError, ValueError):
    """An optimisation request is invalid or could not be satisfied."""


class PluginError(TrialforgeError):
    """A plugin registration or lookup failed."""
