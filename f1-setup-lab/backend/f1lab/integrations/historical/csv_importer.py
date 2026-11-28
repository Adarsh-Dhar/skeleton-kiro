"""CSV Upload importer: inspection, classification and column mapping (Requirements 2, 3, 4).

Pure, side-effect-free helpers. Nothing here writes to the repository; a rejected mapping or file never
changes previously stored state (Req 2.4, 2.5, 4.4, 4.5).
"""
from __future__ import annotations

import csv
import math
import os
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from db.models.f1 import LAP_COLUMNS, TELEMETRY_COLUMNS

SAMPLE_ROW_LIMIT = 20

LAP_SUMMARY = "lap_summary"
SAMPLED_TELEMETRY = "sampled_telemetry"
AMBIGUOUS = "ambiguous"
UNRECOGNIZED = "unrecognized"
CsvKind = Literal["lap_summary", "sampled_telemetry", "ambiguous", "unrecognized"]

LAP_REQUIRED_COLUMNS: tuple[str, ...] = ("lap", "sector_1_s", "sector_2_s", "sector_3_s")
LAP_OPTIONAL_COLUMNS: tuple[str, ...] = ("lap_time_s", "driver", "sector_4_s")
TIME_COLUMNS: tuple[str, ...] = ("timestamp", "session_time_s")
# Positive-number lap fields validated in lap summaries (Req 2.5)
_LAP_NUMERIC_FIELDS = ("sector_1_s", "sector_2_s", "sector_3_s", "sector_4_s", "lap_time_s")
_CHANNEL_COLUMNS: tuple[str, ...] = (
    "lap_number", "distance_m", "speed_kph", "throttle_pct", "brake_pct", "steer_pct", "gear", "rpm", "drs",
    "brake_applied",
)

# Fields a Column_Mapping may target: the historical store's columns plus `timestamp` and the `lap` alias.
STANDARD_FIELDS: tuple[str, ...] = tuple(
    dict.fromkeys(("timestamp", "lap", *TELEMETRY_COLUMNS, *LAP_COLUMNS))
)
_FIELD_ALIASES = {"lap": "lap_number"}


def _norm(name: object) -> str:
    return str(name).strip().lower()


# --- inspection (Req 4.1) ----------------------------------------------------


@dataclass
class Inspection:
    columns: list[str]  # header cells in file order
    row_count: int  # data rows, header excluded
    sample_rows: list[list[str]] = field(default_factory=list)  # first 20 data rows (all if fewer)

    def to_dict(self) -> dict[str, Any]:
        return {"columns": self.columns, "row_count": self.row_count, "sample_rows": self.sample_rows}


def _data_rows(path: str | Path) -> Iterator[list[str]]:
    """Stream data rows (header skipped, truly blank lines skipped, short rows padded to header width)."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            return
        width = len(header)
        for row in reader:
            if not row:
                continue
            yield row + [""] * (width - len(row)) if len(row) < width else row


def read_header(path: str | Path) -> list[str]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return next(csv.reader(fh), None) or []


def inspect(path: str | Path) -> Inspection:
    """Column names in file order, data-row count (streamed) and the first 20 data rows."""
    columns = read_header(path)
    if not columns:
        return Inspection([], 0, [])
    count = 0
    sample: list[list[str]] = []
    for row in _data_rows(path):
        if count < SAMPLE_ROW_LIMIT:
            sample.append(row)
        count += 1
    return Inspection(columns, count, sample)


# --- classification (Req 2.1, 3.1, 3.8) ---------------------------------------


def classify(columns: Iterable[str]) -> CsvKind:
    """Case-insensitive, whitespace-trimmed classification from header names alone."""
    names = {_norm(c) for c in columns}
    lap = all(c in names for c in LAP_REQUIRED_COLUMNS)
    sampled = any(c in names for c in TIME_COLUMNS) and any(c in names for c in _CHANNEL_COLUMNS)
    if lap and sampled:
        return AMBIGUOUS
    if lap:
        return LAP_SUMMARY
    if sampled:
        return SAMPLED_TELEMETRY
    return UNRECOGNIZED


# --- mapping suggestion (Req 4.2) ---------------------------------------------


def suggest_mapping(columns: Iterable[str]) -> dict[str, str]:
    """{standard_field: column} by exact case-insensitive match after trimming; no match -> unmapped.

    Each column and each field is used at most once. When a file has both `lap` and `lap_number`, only
    `lap_number` is suggested (they are the same field).
    """
    by_name: dict[str, str] = {}
    for c in columns:
        by_name.setdefault(_norm(c), str(c).strip())
    out: dict[str, str] = {}
    for fld in STANDARD_FIELDS:
        col = by_name.get(fld)
        if col is None:
            continue
        if fld == "lap" and "lap_number" in by_name:
            continue
        out[fld] = col
    return out


# --- mapping validation (Req 4.3-4.5) -----------------------------------------


@dataclass
class MappingResult:
    ok: bool
    mapping: dict[str, str]  # the accepted mapping, or `previous` (unchanged) when rejected
    errors: list[str] = field(default_factory=list)
    duplicate_fields: list[str] = field(default_factory=list)
    duplicate_columns: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)


def _pairs(mapping: Mapping[str, Any] | Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Flatten {field: column | [columns]} (or an iterable of pairs) into (field, column) pairs, blanks dropped."""
    items = mapping.items() if isinstance(mapping, Mapping) else list(mapping)
    out: list[tuple[str, str]] = []
    for fld, cols in items:
        many = cols if isinstance(cols, (list, tuple, set, frozenset)) else [cols]
        for col in many:
            if col is None or str(col).strip() == "":
                continue
            out.append((str(fld).strip(), str(col).strip()))
    return out


def validate_mapping(
    mapping: Mapping[str, Any] | Iterable[tuple[str, str]],
    columns: Iterable[str],
    previous: Mapping[str, str] | None = None,
) -> MappingResult:
    """Accept a mapping only if it is 1:1 and every column exists in the file.

    Never mutates `mapping` or `previous`. On rejection `result.mapping` is a copy of `previous`
    (or empty), so a stored mapping is replaced only by an accepted one (Req 4.4, 4.5, 4.6).
    """
    kept = dict(previous or {})
    cols = {str(c).strip() for c in columns}
    pairs = _pairs(mapping)

    unknown = sorted({f for f, _ in pairs if f not in STANDARD_FIELDS})
    by_field: dict[str, set[str]] = {}
    by_col: dict[str, set[str]] = {}
    for f, c in pairs:
        canon = _FIELD_ALIASES.get(f, f)
        by_field.setdefault(canon, set()).add(f"{f}\0{c}")
        by_col.setdefault(c, set()).add(f)
    # a field is duplicated when it receives more than one distinct (spelling, column) assignment
    dup_fields = sorted(f for f, v in by_field.items() if len(v) > 1)
    dup_cols = sorted(c for c, fs in by_col.items() if len({_FIELD_ALIASES.get(f, f) for f in fs}) > 1)
    missing = sorted({c for _, c in pairs if c not in cols})

    errors: list[str] = []
    if dup_fields:
        errors.append("Standard field mapped to more than one column: " + ", ".join(dup_fields))
    if dup_cols:
        errors.append("Column mapped to more than one Standard field: " + ", ".join(dup_cols))
    if missing:
        errors.append("Mapped column not found in file: " + ", ".join(missing))
    if unknown:
        errors.append("Unknown Standard field: " + ", ".join(unknown))
    if errors:
        return MappingResult(False, kept, errors, dup_fields, dup_cols, missing, unknown)
    return MappingResult(True, {f: c for f, c in pairs})


# --- lap summary checks (Req 2.4, 2.5) ----------------------------------------


def missing_lap_summary_columns(columns: Iterable[str]) -> list[str]:
    """Every required Lap_Summary_CSV column absent from the header, in canonical order."""
    names = {_norm(c) for c in columns}
    return [c for c in LAP_REQUIRED_COLUMNS if c not in names]


@dataclass
class RowError:
    row: int  # 1-based data row, header excluded
    column: str
    value: str
    reason: str

    def message(self) -> str:
        return f"row {self.row}, column {self.column!r}: invalid value {self.value!r} ({self.reason})"


@dataclass
class LapSummaryValidation:
    ok: bool
    missing_columns: list[str] = field(default_factory=list)
    first_error: RowError | None = None
    row_count: int = 0

    @property
    def error(self) -> str | None:
        if self.missing_columns:
            return "Lap summary is missing required column(s): " + ", ".join(self.missing_columns)
        return self.first_error.message() if self.first_error else None


def _positive_int(text: str) -> bool:
    try:
        v = float(text.strip())
    except ValueError:
        return False
    return math.isfinite(v) and v >= 1 and v == int(v)


def _positive_number(text: str) -> bool:
    try:
        v = float(text.strip())
    except ValueError:
        return False
    return math.isfinite(v) and v > 0


def first_invalid_lap_cell(
    rows: Iterable[Iterable[str]], columns: list[str]
) -> RowError | None:
    """First invalid cell, scanning rows top-down and columns in file order; None when all are valid.

    Required `lap`/sector 1-3 cells must be valid; an empty cell in an optional column (`lap_time_s`,
    `sector_4_s`) is a missing value, not an error (Req 2.2). Other columns are not checked.
    """
    index = {_norm(c): i for i, c in enumerate(columns)}
    checks: list[tuple[int, str, bool]] = []  # (position, canonical name, required)
    for name in ("lap", *_LAP_NUMERIC_FIELDS):
        if name in index:
            checks.append((index[name], name, name in LAP_REQUIRED_COLUMNS))
    checks.sort()
    for n, row in enumerate(rows, start=1):
        cells = list(row)
        for pos, name, required in checks:
            text = cells[pos] if pos < len(cells) else ""
            if not required and text.strip() == "":
                continue
            if name == "lap":
                ok = _positive_int(text)
                reason = "lap must be a positive integer"
            else:
                ok = _positive_number(text)
                reason = "must be a number greater than zero"
            if not ok:
                return RowError(n, columns[pos].strip(), text, reason)
    return None


def validate_lap_summary(path: str | Path) -> LapSummaryValidation:
    """Required-column check, then first-bad-cell row validation, streaming the file."""
    columns = read_header(path)
    missing = missing_lap_summary_columns(columns)
    if missing:
        return LapSummaryValidation(False, missing_columns=missing)
    err = first_invalid_lap_cell(_data_rows(path), columns)
    count = sum(1 for _ in _data_rows(path)) if err is None else 0
    return LapSummaryValidation(err is None, first_error=err, row_count=count)


# --- limits and chunked reading (Req 12.1, 12.4, 12.5) ----------------------------------------------------

CHUNK_ROWS = 10_000  # Req 12.1: at most 10,000 rows per chunk
# Configured limits (Req 12.4, 12.5). Defaults leave room for the 1,000,000-row import of Req 12.2.
CSV_MAX_UPLOAD_BYTES = int(os.environ.get("CSV_MAX_UPLOAD_BYTES", 250 * 1024 * 1024))
CSV_MAX_ROWS = int(os.environ.get("CSV_MAX_ROWS", 2_000_000))
MAX_REPORTED_ERRORS = 200  # Req 6.11


class CsvLimitError(ValueError):
    """A file exceeds the configured size or row-count limit; names the limit and its configured value."""

    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    TOO_MANY_ROWS = "TOO_MANY_ROWS"

    def __init__(self, code: str, limit_name: str, limit: int, actual: int | None = None):
        self.code, self.limit_name, self.limit, self.actual = code, limit_name, limit, actual
        super().__init__(f"file exceeds the {limit_name} limit of {limit}" + (f" (found {actual})" if actual is not None else ""))


def check_file_size(path: str | Path, max_bytes: int | None = None) -> int:
    """Reject an oversized file before any row is read (Req 12.4). Returns the size in bytes."""
    limit = CSV_MAX_UPLOAD_BYTES if max_bytes is None else max_bytes
    size = os.path.getsize(path)
    if size > limit:
        raise CsvLimitError(CsvLimitError.FILE_TOO_LARGE, "maximum file size (bytes)", limit, size)
    return size


def read_chunks(
    path: str | Path, chunksize: int = CHUNK_ROWS, *, max_bytes: int | None = None, max_rows: int | None = None
) -> Iterator[pd.DataFrame]:
    """Yield the file as all-string DataFrames of at most `CHUNK_ROWS` rows (Req 12.1).

    The size limit is checked here, eagerly, before the iterator is returned, so no row is processed for an
    oversized file. The row limit stops reading as soon as it is exceeded (Req 12.5); the offending chunk is not
    yielded. Chunk indexes are continuous, so `index + 1` is the 1-based data row number. Empty cells stay "".
    """
    check_file_size(path, max_bytes)
    size = max(1, min(int(chunksize), CHUNK_ROWS))
    limit = CSV_MAX_ROWS if max_rows is None else max_rows
    return _iter_chunks(path, size, limit)


def _iter_chunks(path: str | Path, size: int, limit: int) -> Iterator[pd.DataFrame]:
    try:
        reader = pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False, encoding="utf-8-sig",
                             chunksize=size)
    except pd.errors.EmptyDataError:
        return
    seen = 0
    try:
        for chunk in reader:
            seen += len(chunk)
            if seen > limit:
                raise CsvLimitError(CsvLimitError.TOO_MANY_ROWS, "maximum row count", limit)
            chunk.columns = [str(c).strip() for c in chunk.columns]
            yield chunk.fillna("")
    except pd.errors.EmptyDataError:
        return
    finally:
        reader.close()


# --- row validation (Req 3.9, 6.1-6.12) ---------------------------------------------------------------

_PERCENT_RANGES: dict[str, tuple[float, float]] = {
    "throttle_pct": (0.0, 100.0), "brake_pct": (0.0, 100.0), "steer_pct": (-100.0, 100.0),
}
_NUMERIC_SAMPLE_FIELDS: tuple[str, ...] = (
    "distance_m", "speed_kph", "throttle_pct", "brake_pct", "steer_pct", "gear", "rpm", "drs", "brake_applied",
)
# Allowed DRS codes: the OpenF1 documented state codes (0 and 1 cover plain off/on style exports).
DRS_ALLOWED: frozenset[int] = frozenset({0, 1, 2, 3, 8, 9, 10, 12, 14})


class _Collector:
    """Counts every finding but keeps only the first `cap` entries (Req 6.11)."""

    def __init__(self, cap: int = MAX_REPORTED_ERRORS):
        self.cap, self.count, self.items = cap, 0, []  # type: int, int, list[dict]

    def add(self, row: int, column: str, value: object, reason: str, severity: str, **extra: Any) -> None:
        self.count += 1
        if len(self.items) < self.cap:
            self.items.append({"row": row, "column": column, "value": value, "reason": reason,
                               "severity": severity, **extra})

    def add_rows(self, rows: Iterable[int], cells: Any, column: str, reason: str, severity: str) -> None:
        for r in rows:
            if len(self.items) >= self.cap:
                self.count += 1
            else:
                self.add(int(r) + 1, column, cells[int(r)], reason, severity)


@dataclass
class ValidationReport:
    ok: bool  # True when there is no blocking error (Req 6.11)
    total_rows: int = 0
    samples: int = 0  # rows kept after skipped-time rows and dropped duplicates
    missing_counts: dict[str, int] = field(default_factory=dict)  # per mapped column (Req 6.1)
    duplicate_groups: list[list[int]] = field(default_factory=list)  # 1-based rows per (time, lap) group (Req 6.2)
    duplicate_group_count: int = 0
    duplicate_rows_affected: int = 0
    duplicates_dropped: int = 0  # drop_exact_duplicates (Req 6.7)
    out_of_order_rows: list[int] = field(default_factory=list)  # Req 6.3
    out_of_order_count: int = 0
    rows_moved: int = 0  # sort policy (Req 6.9)
    skipped_time_rows: int = 0  # Req 3.9
    errors: list[dict] = field(default_factory=list)  # blocking, capped at 200
    error_count: int = 0  # true total of blocking errors
    warnings: list[dict] = field(default_factory=list)  # non-blocking findings, capped at 200
    warning_count: int = 0
    coverage: dict[str, dict[str, float]] = field(default_factory=dict)  # Req 6.12

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _numeric(cells: pd.Series) -> tuple[pd.Series, pd.Series]:
    """(finite float or NaN, blank mask) for a string column."""
    text = cells.astype(str).str.replace("\u00a0", "", regex=False).str.strip()
    blank = text == ""
    num = pd.to_numeric(text.where(~blank), errors="coerce")
    return num.where(np.isfinite(num)), blank


def validate_rows(
    frame: Any, mapping: Mapping[str, Any], units: dict | None = None, policy: dict | None = None
) -> ValidationReport:
    """Validate sample rows against the Column_Mapping and report findings without changing any data.

    `frame` holds raw cells (strings); the row number reported is the 1-based data row. `policy` keys:
    `duplicate_policy` (`drop_exact_duplicates` | `reject`), `order_policy` (`sort` | `reject`), `data_type`,
    `tz_offset`, `timezone`. An unset policy defaults to `reject` (nothing is changed silently).

    Blocking errors: unusable time column, rejected duplicates / ordering, percentage range, gear and DRS
    violations. Non-numeric and empty cells, unparseable times (skipped rows) and invalid lap numbers are stored
    as missing by the normalizer and reported as warnings (Req 3.5, 5.8, 5.10).
    """
    from f1lab.integrations.historical import csv_normalizer as norm

    policy = dict(policy or {})
    dup_policy = policy.get("duplicate_policy") or "reject"
    order_policy = policy.get("order_policy") or "reject"
    for name, val, allowed in (("duplicate_policy", dup_policy, norm.DUPLICATE_POLICIES),
                               ("order_policy", order_policy, norm.ORDER_POLICIES)):
        if val not in allowed:
            raise ValueError(f"unknown {name} {val!r}; supported: {', '.join(allowed)}")
    u = norm.normalize_units(units)
    m = norm._canonical_mapping(dict(mapping))
    df = norm._as_frame(frame)
    df.columns = [str(c).strip() for c in df.columns]
    df = df.astype(str).reset_index(drop=True)
    n = len(df)
    errs, warns = _Collector(), _Collector()
    rep = ValidationReport(ok=True, total_rows=n)

    present = {fld: col for fld, col in m.items() if col in df.columns}
    for fld, col in m.items():
        if col not in df.columns:
            errs.add(0, col, None, "mapped_column_missing", "error")

    def finish(retained: pd.Series | None = None) -> ValidationReport:
        rep.errors, rep.error_count = errs.items, errs.count
        rep.warnings, rep.warning_count = warns.items, warns.count
        rep.ok = errs.count == 0
        _coverage(rep, df, present, parsed, retained)
        return rep

    parsed: dict[str, pd.Series] = {}  # field -> numeric series with NaN for missing/invalid

    # 6.1 missing counts per mapped column
    for col in dict.fromkeys(present.values()):
        rep.missing_counts[col] = int((df[col].str.strip() == "").sum())

    # numeric sample fields: 6.4 non-numeric, 6.5 ranges, 6.6 gear / DRS
    for fld in _NUMERIC_SAMPLE_FIELDS:
        if fld not in present:
            continue
        col = present[fld]
        num, blank = _numeric(df[col])
        parsed[fld] = num
        raw = df[col].tolist()
        warns.add_rows(np.flatnonzero((~blank & num.isna()).to_numpy()), raw, col, "non_numeric", "warning")
        valid = num.notna().to_numpy()
        values = num.to_numpy()
        bad: np.ndarray | None = None
        reason = ""
        if fld in _PERCENT_RANGES:
            lo, hi = _PERCENT_RANGES[fld]
            with np.errstate(invalid="ignore"):
                bad, reason = valid & ((values < lo) | (values > hi)), "out_of_range"
        elif fld == "gear":
            with np.errstate(invalid="ignore"):
                bad, reason = valid & (values != np.floor(values)), "non_integer_gear"
        elif fld == "drs":
            with np.errstate(invalid="ignore"):
                bad = valid & ~(np.isin(values, list(DRS_ALLOWED)))
            reason = "drs_not_allowed"
        if bad is not None:
            errs.add_rows(np.flatnonzero(bad), raw, col, reason, "error")

    # lap_number (5.10): integer >= 1, otherwise reported and unavailable
    lap = pd.Series(np.full(n, np.nan))
    if "lap_number" in present:
        col = present["lap_number"]
        num, blank = _numeric(df[col])
        ok = num.notna() & (num >= 1) & (num == np.floor(num))
        lap = num.where(ok)
        parsed["lap_number"] = lap
        warns.add_rows(np.flatnonzero((~ok).to_numpy()), df[col].tolist(), col, "invalid_lap_number", "warning")

    if policy.get("data_type") == norm.LAP_SUMMARY:  # lap summaries have no samples to order or deduplicate
        rep.samples = n
        return finish()

    # time (3.6, 3.9, 5.8)
    t = pd.Series(np.full(n, np.nan))
    tcol = present.get("session_time_s") or present.get("timestamp")
    if tcol is None:
        errs.add(0, "timestamp", None, "no time column mapped (timestamp or session_time_s)", "error")
        return finish(pd.Series(np.zeros(len(df), dtype=bool)))
    if "session_time_s" in present:
        num, _ = _numeric(df[tcol])
        t = num / 1000.0 if u["time"] == "milliseconds" else num
    else:
        st = norm.to_session_time(df[tcol].tolist(), policy.get("tz_offset"), policy.get("timezone"))
        t = pd.Series([math.nan if v is None else v for v in st.session_time_s], dtype="float64")
    usable = t.notna().to_numpy()
    if not usable.any():
        errs.add(0, tcol, None, "time column has no usable values", "error")
        return finish(pd.Series(np.zeros(len(df), dtype=bool)))
    skipped = np.flatnonzero(~usable)
    rep.skipped_time_rows = len(skipped)
    warns.add_rows(skipped, df[tcol].tolist(), tcol, "unparseable_time", "warning")
    parsed["session_time_s"] = t

    retained = pd.Series(usable)
    tv, lv = t.to_numpy(), lap.fillna(-1).to_numpy()

    # 6.2 duplicates: same time and same lap
    groups: list[np.ndarray] = []
    key = pd.DataFrame({"t": tv, "lap": lv})[usable]
    for _, idx in key.groupby(["t", "lap"], sort=False).indices.items():
        if len(idx) > 1:
            groups.append(np.flatnonzero(usable)[idx])
    rep.duplicate_group_count = len(groups)
    rep.duplicate_groups = [(g + 1).tolist() for g in groups[:MAX_REPORTED_ERRORS]]
    rep.duplicate_rows_affected = int(sum(len(g) for g in groups))
    mapped_cols = list(dict.fromkeys(present.values()))
    if dup_policy == "drop_exact_duplicates":
        exact = df.duplicated(subset=mapped_cols, keep="first").to_numpy() & usable
        rep.duplicates_dropped = int(exact.sum())
        retained = pd.Series(usable & ~exact)
        left = retained.to_numpy()
        for g in groups:  # groups that still differ in some mapped value remain, reported but not blocking
            if left[g].sum() > 1:
                warns.add(int(g[0]) + 1, tcol, None, "duplicate_time_and_lap", "warning", rows=(g + 1).tolist())
    else:
        for g in groups:
            errs.add(int(g[0]) + 1, tcol, None, "duplicate_time_and_lap", "error", rows=(g + 1).tolist())

    # 6.3 out of order: lower than the preceding sample of the same lap
    keep_idx = np.flatnonzero(retained.to_numpy())
    ooo: list[int] = []
    moved = 0
    sub = pd.DataFrame({"lap": lv[keep_idx]})
    for _, pos in sub.groupby("lap", sort=False).indices.items():
        idx = keep_idx[pos]
        tt = tv[idx]
        if len(tt) < 2:
            continue
        ooo.extend(idx[1:][tt[1:] < tt[:-1]].tolist())
        order = np.argsort(tt, kind="stable")
        moved += int((order != np.arange(len(tt))).sum())
    ooo.sort()
    rep.out_of_order_count = len(ooo)
    rep.out_of_order_rows = [r + 1 for r in ooo[:MAX_REPORTED_ERRORS]]
    if order_policy == "sort":
        rep.rows_moved = moved
        warns.add_rows(ooo, df[tcol].tolist(), tcol, "out_of_order", "warning")
    else:
        errs.add_rows(ooo, df[tcol].tolist(), tcol, "out_of_order", "error")

    rep.samples = int(retained.sum())
    return finish(retained)


def _coverage(rep: ValidationReport, df: pd.DataFrame, present: dict[str, str],
              parsed: dict[str, pd.Series], retained: pd.Series | None) -> None:
    """Samples with a value per Standard_Field and its percentage of all samples, 1 decimal (Req 6.12)."""
    keep = np.ones(len(df), dtype=bool) if retained is None else retained.to_numpy()
    total = int(keep.sum())
    rep.samples = total if retained is not None else rep.samples
    for fld in dict.fromkeys((*TELEMETRY_COLUMNS, *present)):
        if fld in parsed:
            count = int(parsed[fld].notna().to_numpy()[keep].sum())
        elif fld in present and fld not in ("timestamp", "driver"):
            count = int(((df[present[fld]].str.strip() != "").to_numpy())[keep].sum()) if fld in LAP_COLUMNS else 0
        else:
            count = 0
        rep.coverage[fld] = {"count": count, "percent": round(count / total * 100, 1) if total else 0.0}



# --- import orchestration (Req 1.2, 1.3, 1.8, 2.6-2.8, 7.1-7.8, 8.1, 8.3, 9.4, 10.1-10.3, 12.3, 12.7) ----------------------
import re  # noqa: E402
from collections.abc import Callable  # noqa: E402
from datetime import date  # noqa: E402

from f1lab.integrations.historical import csv_normalizer as _norm_mod  # noqa: E402
from f1lab.integrations.historical.importer import (  # noqa: E402
    DRIVER_NOT_FOUND,
    DUPLICATE_KEY_CONFLICT,
    DUPLICATE_SESSION,
    INVALID_CSV_STRUCTURE,
    INVALID_REQUEST,
    METADATA_INVALID,
    ROW_VALIDATION_FAILED,
    UNIT_NOT_SUPPORTED,
    UPLOAD_NOT_FOUND,
    HistoricalImportError,
    ImportCancelled,
)
from f1lab.integrations.historical.repository import (  # noqa: E402
    HistoricalRepository,
)
from f1lab.integrations.historical.repository import session_id as _make_session_id  # noqa: E402

METADATA_ITEMS: tuple[str, ...] = ("provider", "year", "event", "circuit", "session_type", "driver", "session_date",
                                   "timezone", "original_filename", "time_unit", "distance_unit")  # Req 7.1
_FREE_TEXT_ITEMS = ("event", "circuit", "session_type", "driver")
MAX_TEXT_LENGTH = 200
MAX_FILENAME_LENGTH = 255
MIN_YEAR, MAX_YEAR = 1950, 2100
UNKNOWN_DRIVER = "UNKNOWN"  # storage key only; never written to Session_Metadata
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LAP_REQUIRED_FIELDS = ("lap_number", "sector_1_s", "sector_2_s", "sector_3_s")


def _present(value: Any) -> bool:
    return value is not None and not (isinstance(value, str) and value.strip() == "")


def validate_session_metadata(meta: Mapping[str, Any] | None, original_filename: str | None = None,
                              units: Mapping[str, str | None] | None = None) -> dict[str, Any]:
    """Validate the 11 Session_Metadata items and return them with their Unavailable_Markers (Req 7.1-7.5).

    ``provider`` is always ``User CSV``. ``original_filename`` is the uploaded file's name. ``units`` maps ``time`` and
    ``distance`` to the supplied unit (or None). An item that is not supplied is accepted only when it is listed in
    ``meta['unavailable']``; it is then stored as None and named in ``unavailable``. Nothing is defaulted or guessed.
    Raises ``HistoricalImportError(METADATA_INVALID)`` naming every missing item and every invalid item with its rule.
    """
    meta = dict(meta or {})
    marked = meta.get("unavailable") or []
    marked_set = {str(m) for m in marked} if isinstance(marked, (list, tuple, set, frozenset)) else set()
    supplied_units = dict(units or {})
    values: dict[str, Any] = {
        "year": meta.get("year"), "event": meta.get("event"), "circuit": meta.get("circuit"),
        "session_type": meta.get("session_type"), "driver": meta.get("driver"), "session_date": meta.get("session_date"),
        "timezone": meta.get("timezone"),
        "original_filename": original_filename if _present(original_filename) else meta.get("original_filename"),
        "time_unit": supplied_units.get("time"), "distance_unit": supplied_units.get("distance"),
    }
    missing: list[str] = []
    invalid: list[dict[str, str]] = []
    out: dict[str, Any] = {"provider": _norm_mod.PROVIDER_LABEL}
    unavailable: list[str] = []

    for bad in sorted(marked_set - set(METADATA_ITEMS)):
        invalid.append({"item": bad, "rule": "unknown item in the unavailable list"})
    if "provider" in marked_set:
        invalid.append({"item": "provider", "rule": "provider is fixed to 'User CSV' and cannot be marked unavailable"})

    for item in METADATA_ITEMS[1:]:
        value = values[item]
        if not _present(value):
            if item in marked_set:
                out[item] = None
                unavailable.append(item)
            else:
                missing.append(item)
                out[item] = None
            continue
        rule = None
        if item == "year":
            ok = isinstance(value, int) and not isinstance(value, bool)
            if not ok and isinstance(value, str) and re.fullmatch(r"\d{1,6}", value.strip()):
                value, ok = int(value.strip()), True
            if not ok or not MIN_YEAR <= value <= MAX_YEAR:
                rule = f"year must be an integer from {MIN_YEAR} to {MAX_YEAR}"
        elif item == "session_date":
            text = value.strip() if isinstance(value, str) else ""
            try:
                if not _ISO_DATE_RE.match(text):
                    raise ValueError
                date.fromisoformat(text)
                value = text
            except ValueError:
                rule = "session date must be a valid calendar date in ISO 8601 YYYY-MM-DD format"
        elif item == "timezone":
            try:
                if not isinstance(value, str) or _norm_mod.resolve_timezone(value) is None:
                    raise ValueError
                value = value.strip()
            except ValueError:
                rule = "timezone must be a valid IANA timezone name"
        elif item == "original_filename":
            if not isinstance(value, str) or len(value) > MAX_FILENAME_LENGTH:
                rule = f"original filename must be text of at most {MAX_FILENAME_LENGTH} characters"
        elif item in ("time_unit", "distance_unit"):
            pass  # already validated by the unit conversion tables
        elif item in _FREE_TEXT_ITEMS:
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_TEXT_LENGTH:
                rule = f"{item} must be text of 1 to {MAX_TEXT_LENGTH} characters after trimming whitespace"
            else:
                value = value.strip()
        if rule:
            invalid.append({"item": item, "rule": rule})
        out[item] = value
    if missing or invalid:
        parts = []
        if missing:
            parts.append("missing item(s) neither supplied nor marked unavailable: " + ", ".join(missing))
        if invalid:
            parts.append("invalid item(s): " + "; ".join(f"{i['item']} ({i['rule']})" for i in invalid))
        raise HistoricalImportError(METADATA_INVALID, "Session metadata rejected. " + ". ".join(parts),
                                    {"missing": missing, "invalid": invalid})
    out["unavailable"] = [i for i in METADATA_ITEMS if i in unavailable]
    return out


def _resolve_units(csv_req: Mapping[str, Any], meta: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, str | None]]:
    """(normalized units, {time, distance: supplied unit or None}). An unsupported unit raises UNIT_NOT_SUPPORTED (Req 5.4)."""
    raw = dict(csv_req.get("units") or {})
    supplied: dict[str, Any] = {}
    for key in ("time", "speed", "distance"):
        val = raw.get(key, raw.get(f"{key}_unit"))
        if val is None:
            val = meta.get(f"{key}_unit")
        if val is not None:
            supplied[key] = val
    try:
        units = _norm_mod.normalize_units(supplied)
    except _norm_mod.UnsupportedUnitError as exc:
        raise HistoricalImportError(UNIT_NOT_SUPPORTED, str(exc), {"field": exc.field, "supported": list(exc.supported)}) from exc
    return units, {k: (units[k] if k in supplied else None) for k in ("time", "distance")}


def _rows_message(report: ValidationReport, label: str = "") -> str:
    first = report.errors[0] if report.errors else {}
    where = f"row {first.get('row')}, column {first.get('column')!r}: {first.get('reason')}" if first else "see details"
    prefix = f"{label}: " if label else ""
    return f"{prefix}{report.error_count} blocking row error(s); first: {where}"


def _storage_session_id(repo: HistoricalRepository, metadata: dict, file_hash: str, replace_sid: str | None) -> str:
    year, event, stype, driver = metadata["year"], metadata["event"], metadata["session_type"], metadata["driver"]
    if year is None or not event or not stype:
        return f"csv_upload-{file_hash[:16]}"
    sid = _make_session_id(int(year), f"{event} {driver}" if driver else event, stype, _norm_mod.PROVIDER)
    if sid != replace_sid and repo.exists(sid):
        sid = f"{sid}-{file_hash[:8]}"  # a different session slugs to the same id: never overwrite it
    return sid


def import_session_csv(repo: HistoricalRepository, payload: dict, progress: Callable[[str, str], None] | None = None,
                       should_cancel: Callable[[], bool] | None = None, *, upload_dir: str | Path | None = None,
                       **_ctx: Any) -> dict:
    """Import one staged CSV upload synchronously (call via asyncio.to_thread). Returns the stored manifest.

    ``payload['csv']`` holds ``file_hash``, ``mapping``, ``units``, ``metadata``, ``duplicate_policy``, ``order_policy``,
    ``data_type`` and optionally ``replace_existing`` and ``tz_offset``. Nothing is written until the single
    ``repo.save_session`` call at the end, which also stores the column mapping and file provenance, so any earlier failure
    or cancellation leaves the repository unchanged (Req 1.8, 8.1, 12.7).
    """
    say = progress or (lambda *_: None)

    def checkpoint() -> None:
        if should_cancel and should_cancel():
            raise ImportCancelled("import cancelled")

    csv_req = dict(payload.get("csv") or {})
    file_hash = str(csv_req.get("file_hash") or "").strip().lower()
    if not _HASH_RE.match(file_hash):
        raise HistoricalImportError(INVALID_REQUEST, "csv.file_hash must be a 64-character hexadecimal SHA-256")
    directory = Path(upload_dir) if upload_dir else Path(os.environ.get("F1LAB_DATA_DIR", "data")) / "csv-uploads"
    path = directory / f"{file_hash}.csv"
    if not path.is_file():
        raise HistoricalImportError(UPLOAD_NOT_FOUND, "the uploaded file is no longer available; upload the file again")
    checkpoint()
    say("PROCESSING", "checking session metadata")

    # ---- metadata, units, policies (Req 7.1-7.5)
    meta_in = dict(csv_req.get("metadata") or {})
    units, supplied_units = _resolve_units(csv_req, meta_in)
    upload = repo.upload(file_hash)
    filename = (upload or {}).get("original_filename") or meta_in.get("original_filename")
    metadata = validate_session_metadata(meta_in, filename, supplied_units)
    dup_policy = csv_req.get("duplicate_policy") or "reject"
    order_policy = csv_req.get("order_policy") or "reject"
    for name, val, allowed in (("duplicate_policy", dup_policy, _norm_mod.DUPLICATE_POLICIES),
                               ("order_policy", order_policy, _norm_mod.ORDER_POLICIES)):
        if val not in allowed:
            raise HistoricalImportError(INVALID_REQUEST, f"unknown {name} {val!r}; supported: {', '.join(allowed)}")

    # ---- duplicates before any processing (Req 10.1-10.3)
    def check_duplicates() -> str | None:
        same_file = repo.find_by_file_hash(file_hash)
        if same_file:
            raise HistoricalImportError(DUPLICATE_SESSION, f"this file was already imported as session {same_file}",
                                        {"session_id": same_file})
        key = {n: metadata[n] for n in ("provider", "event", "session_type", "driver", "year")}
        if any(v is None for v in key.values()):
            return None  # a key with an unavailable part cannot identify another session
        found = repo.find_by_key(key)
        if found and not csv_req.get("replace_existing"):
            raise HistoricalImportError(DUPLICATE_KEY_CONFLICT,
                                        f"session {found} has the same provider, event, session type, driver and year; "
                                        "choose 'replace existing session' or cancel", {"session_id": found})
        return found

    replace_sid = check_duplicates()

    # ---- structure (Req 2.4, 3.1, 3.6, 3.8, 4.3-4.5)
    columns = [c.strip() for c in read_header(path)]
    if not columns:
        raise HistoricalImportError(INVALID_CSV_STRUCTURE, "the file has no header row")
    kind = csv_req.get("data_type") or classify(columns)
    if kind == AMBIGUOUS:
        raise HistoricalImportError(INVALID_CSV_STRUCTURE, "the file has both lap summary and sampled telemetry columns; choose a data type")
    if kind not in (LAP_SUMMARY, SAMPLED_TELEMETRY):
        raise HistoricalImportError(INVALID_CSV_STRUCTURE, "the file is neither a lap summary nor sampled telemetry")
    requested = csv_req.get("mapping") or suggest_mapping(columns)
    result = validate_mapping(requested, columns)
    if not result.ok:
        raise HistoricalImportError(INVALID_CSV_STRUCTURE, "; ".join(result.errors), {"errors": result.errors})
    mapping = _norm_mod._canonical_mapping(result.mapping)
    if kind == LAP_SUMMARY:
        gone = [("lap" if f == "lap_number" else f) for f in _LAP_REQUIRED_FIELDS if f not in mapping]
        if gone:
            raise HistoricalImportError(INVALID_CSV_STRUCTURE, "Lap summary is missing required column(s): " + ", ".join(gone),
                                        {"missing_columns": gone})
    elif "session_time_s" not in mapping and "timestamp" not in mapping:
        raise HistoricalImportError(INVALID_CSV_STRUCTURE, "no time column is mapped (timestamp or session_time_s)")

    # ---- chunked read, driver filter, lap-summary cell checks (Req 7.6, 7.7, 12.1, 12.3)
    try:
        check_file_size(path)  # before any row is read (Req 12.4)
        total = sum(1 for _ in _data_rows(path))
        if total == 0:
            raise HistoricalImportError(INVALID_CSV_STRUCTURE, "the file has no data rows")
        # at most 10,000 rows per chunk, and at least 20 chunks so progress is reported every 5 percent (Req 12.1, 12.3)
        chunks = read_chunks(path, max(1, min(CHUNK_ROWS, -(-total // 20))))
        driver_col = mapping.get("driver")
        selected = metadata["driver"]
        wanted = list(dict.fromkeys(mapping.values()))
        canon_of = {col: ("lap" if fld == "lap_number" else fld) for fld, col in mapping.items()}
        pieces: list[pd.DataFrame] = []
        done = kept = 0
        last_bucket = -1
        say("PROCESSING", f"0% (0/{total} rows)")
        for chunk in chunks:
            checkpoint()
            part = chunk
            if driver_col and selected:
                part = part[part[driver_col] == selected]
            part = part[wanted]
            if kind == LAP_SUMMARY and len(part):
                names = [canon_of[c] for c in wanted]
                err = first_invalid_lap_cell(part.values.tolist(), names)
                if err is not None:
                    orig = int(part.index[err.row - 1]) + 1
                    col = next(c for c in wanted if canon_of[c] == err.column)
                    raise HistoricalImportError(
                        ROW_VALIDATION_FAILED, RowError(orig, col, err.value, err.reason).message(),
                        {"errors": [{"row": orig, "column": col, "value": err.value, "reason": err.reason, "severity": "error"}],
                         "error_count": 1})
            kept += len(part)
            pieces.append(part)
            done += len(chunk)
            bucket = int(done * 20 / total)
            if bucket > last_bucket:
                last_bucket = bucket
                say("PROCESSING", f"{int(done * 100 / total)}% ({done}/{total} rows)")
    except CsvLimitError as exc:
        raise HistoricalImportError(exc.code, str(exc), {"limit": exc.limit_name, "value": exc.limit}) from exc
    if driver_col and selected and kept == 0:
        raise HistoricalImportError(DRIVER_NOT_FOUND, f"no rows match the selected driver {selected!r}")
    frame = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame(columns=wanted)
    del pieces
    excluded = total - kept
    checkpoint()

    # ---- normalize
    say("PROCESSING", "normalizing")
    warnings: list[str] = []
    laps = pd.DataFrame()
    telemetry: dict[str, pd.DataFrame] = {}
    stats = {"skipped_rows": 0, "duplicates_dropped": 0, "out_of_order": 0}
    unavailable_channels: list[str] = []
    coverage: dict[str, Any] = {}
    if kind == LAP_SUMMARY:
        label = _norm_mod.DATA_TYPE_LAPS
        laps = _norm_mod.build_lap_frame(frame, mapping, units)
        if driver_col and not selected:
            codes = laps["driver"].map(lambda v: v if _present(v) else UNKNOWN_DRIVER)
        else:
            codes = pd.Series([selected or UNKNOWN_DRIVER] * len(laps), dtype=object)
        laps["driver"] = codes.astype(object)
        driver_keys = sorted({str(c) for c in laps["driver"]})
        unavailable_channels = list(laps.attrs.get("unavailable", []))
        if "lap_time_s" not in mapping:
            warnings.append("lap time is unavailable for every lap; it is not derived from the sectors")
    else:
        label = _norm_mod.DATA_TYPE_FULL
        if driver_col and not selected:
            keys = frame[driver_col].map(lambda v: v.strip() or UNKNOWN_DRIVER)
            groups = {str(k): frame[keys == k].reset_index(drop=True) for k in dict.fromkeys(keys)}
        else:
            groups = {selected or UNKNOWN_DRIVER: frame}
        policy = {"duplicate_policy": dup_policy, "order_policy": order_policy, "data_type": SAMPLED_TELEMETRY,
                  "tz_offset": csv_req.get("tz_offset"), "timezone": metadata["timezone"]}
        for key, part in groups.items():
            checkpoint()
            report = validate_rows(part, mapping, units, policy)
            if not report.ok:
                raise HistoricalImportError(ROW_VALIDATION_FAILED, _rows_message(report, key if len(groups) > 1 else ""),
                                            {"errors": report.errors, "error_count": report.error_count})
            tel = _norm_mod.build_telemetry_frame(part, mapping, units, tz_offset=csv_req.get("tz_offset"),
                                                  user_tz=metadata["timezone"], duplicate_policy=dup_policy,
                                                  order_policy=order_policy)
            if tel.attrs.get("time_error"):
                raise HistoricalImportError(INVALID_CSV_STRUCTURE, str(tel.attrs["time_error"]))
            telemetry[key] = tel
            for k in stats:
                stats[k] += int(tel.attrs.get(k, 0))
            unavailable_channels = list(tel.attrs.get("unavailable", []))
            coverage[key] = report.coverage
            if tel.attrs.get("timezone_unavailable"):
                warnings.append(f"{key}: timestamps carry no timezone; session time is relative to the first sample")
        driver_keys = sorted(telemetry)
    if stats["skipped_rows"]:
        warnings.append(f"{stats['skipped_rows']} row(s) with an empty or unparseable time were skipped")
    if excluded:
        warnings.append(f"{excluded} row(s) excluded: they do not match the selected driver")
    checkpoint()

    # ---- manifest and the single save (Req 1.3, 2.6, 8.1, 8.3)
    replace_sid = check_duplicates() or replace_sid
    sid = _storage_session_id(repo, metadata, file_hash, replace_sid)
    channels = {"laps": kind == LAP_SUMMARY, "telemetry": {k: not t.empty for k, t in telemetry.items()}, "position": {}}
    manifest = _norm_mod.build_manifest(
        metadata, mapping, units, {"file_hash": file_hash, "original_filename": metadata["original_filename"]}, label,
        session_id=sid, drivers=[{"code": k, "number": "", "name": k, "team": None} for k in driver_keys], channels=channels)
    manifest["metadata"] = metadata  # all 11 items, Unavailable_Markers preserved
    manifest.update(
        corners=[], all_drivers=not selected, warnings=warnings, partial=False, upstream={"provider": "csv_upload"},
        duplicate_policy=dup_policy, order_policy=order_policy,
        import_stats={"rows_total": total, "rows_imported": kept, "rows_excluded": excluded, **stats},
        unavailable_channels=unavailable_channels, coverage=coverage)
    checkpoint()  # last chance: nothing has been written yet
    say("SAVING", f"writing normalized data for {len(driver_keys)} driver(s)")
    repo.save_session(sid, manifest, laps, pd.DataFrame(), telemetry, {},
                      column_mapping={"mapping": mapping, "units": units, "duplicate_policy": dup_policy, "order_policy": order_policy},
                      replace_sid=replace_sid)
    return manifest


__all__ = [
    "AMBIGUOUS", "LAP_SUMMARY", "SAMPLED_TELEMETRY", "UNRECOGNIZED", "STANDARD_FIELDS", "Inspection", "MappingResult",
    "RowError", "LapSummaryValidation", "inspect", "classify", "suggest_mapping", "validate_mapping",
    "missing_lap_summary_columns", "first_invalid_lap_cell", "validate_lap_summary", "read_header",
    "CHUNK_ROWS", "CSV_MAX_UPLOAD_BYTES", "CSV_MAX_ROWS", "MAX_REPORTED_ERRORS", "DRS_ALLOWED", "CsvLimitError",
    "ValidationReport", "check_file_size", "read_chunks", "validate_rows", "METADATA_ITEMS", "validate_session_metadata",
    "import_session_csv",
]
