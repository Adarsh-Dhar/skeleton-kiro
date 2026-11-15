"""Quality-flag taxonomy.

Statuses (exposed via :func:`quality_status`):

* ``invalid``       - unusable (wrong type, non-finite, incompatible unit, duplicate, ...)
* ``outlier``       - statistically unusual; **kept** and flagged, never deleted
* ``sensor_warning``- adapter/sensor raised a warning; kept
* ``accepted``      - no issue

``missing`` is not a flag on a record: it is reported per run/metric as
``missing required metric`` and in ``MeasurementEngine.report``.
"""

from __future__ import annotations

from collections.abc import Iterable

INVALID_FLAGS = frozenset(
    {
        "unit_mismatch",
        "invalid_type",
        "undefined_metric",
        "run_id_mismatch",
        "duplicate",
        "out_of_range",
        "non_finite",
        "unexpected_time_series",
        "missing_sample_index",
        "invalid_timestamp",
    }
)
OUTLIER_FLAGS = frozenset({"statistical_outlier"})
WARNING_FLAGS = frozenset({"sensor_warning", "interpolated", "gap_filled", "out_of_order"})
INFO_FLAGS = frozenset({"unit_converted", "run_id_bound", "late_arrival", "timestamp_assumed_utc"})


def is_usable(flags: Iterable[str]) -> bool:
    """A measurement is usable for analysis unless it carries an invalid flag."""
    return not set(flags) & INVALID_FLAGS


def quality_status(flags: Iterable[str]) -> str:
    """Return the dominant quality status for ``flags``."""
    s = set(flags)
    if s & INVALID_FLAGS:
        return "invalid"
    if s & OUTLIER_FLAGS:
        return "outlier"
    if s & {"sensor_warning", "interpolated", "gap_filled"}:
        return "sensor_warning"
    return "accepted"
