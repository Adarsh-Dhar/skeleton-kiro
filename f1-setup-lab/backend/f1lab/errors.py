"""Errors the API maps to HTTP status codes."""

from __future__ import annotations


class NotFound(KeyError):
    """Unknown session or import (404)."""


class Conflict(RuntimeError):
    """The request is not allowed in the current state (409)."""
