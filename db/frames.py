"""pandas DataFrame <-> table rows, preserving which columns existed and their pandas dtypes.

``write_frame`` stores a frame into a table whose canonical columns are known; columns outside that set are kept per
row in ``extra_json``. ``schema_of`` returns the ``[[name, dtype], ...]`` list to store next to the rows, and
``read_frame`` rebuilds the frame with exactly those columns, in that order, with the original dtypes.
"""
from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Connection, Table, select

CHUNK = 5000


def _py(v: Any) -> Any:
    """numpy/pandas scalar -> plain Python value that sqlite3 can bind (NA/NaN/NaT -> None)."""
    if v is None or v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if isinstance(v, pd.Timedelta):
        return v.total_seconds()
    return v


def _column_values(s: pd.Series) -> list[Any]:
    if pd.api.types.is_float_dtype(s.dtype) and not pd.api.types.is_extension_array_dtype(s.dtype):
        return [None if x != x else x for x in s.to_numpy(dtype=float).tolist()]
    return [_py(x) for x in s.astype(object).tolist()]


def schema_of(df: pd.DataFrame) -> list[list[str]]:
    return [[str(c), str(df[c].dtype)] for c in df.columns]


def write_frame(conn: Connection, table: Table, df: pd.DataFrame, keys: dict[str, Any], canonical: Iterable[str]) -> int:
    """Insert ``df`` rows into ``table`` with constant ``keys`` and a 0-based ``row_idx``. Returns rows written."""
    if df is None or df.empty:
        return 0
    canon = [c for c in canonical if c in df.columns]
    extra = [c for c in df.columns if c not in set(canonical)]
    cols = {c: _column_values(df[c]) for c in [*canon, *extra]}
    names = [*keys.keys(), "row_idx", *canon, "extra_json"]
    placeholders = ", ".join("?" for _ in names)
    sql = f'INSERT INTO {table.name} ({", ".join(chr(34) + n + chr(34) for n in names)}) VALUES ({placeholders})'
    const = list(keys.values())
    n = len(df)
    batch: list[tuple] = []
    for i in range(n):
        ex = {c: cols[c][i] for c in extra} if extra else None
        batch.append((*const, i, *(cols[c][i] for c in canon), json.dumps(ex, default=str) if ex else None))
        if len(batch) >= CHUNK:
            conn.exec_driver_sql(sql, batch)
            batch = []
    if batch:
        conn.exec_driver_sql(sql, batch)
    return n


def _restore(s: pd.Series, dtype: str) -> pd.Series:
    try:
        if dtype in ("boolean", "bool"):
            out = s.map(lambda v: None if v is None or (isinstance(v, float) and math.isnan(v)) else bool(v)).astype("boolean")
            return out.astype(bool) if dtype == "bool" and not out.isna().any() else out
        if dtype in ("Int64", "Int32", "Int16", "Int8"):
            return pd.to_numeric(s, errors="coerce").astype(dtype)
        if dtype.startswith(("int", "uint")):
            num = pd.to_numeric(s, errors="coerce")
            return num.astype(dtype) if not num.isna().any() else num.astype("Int64")
        if dtype.startswith("float"):
            return pd.to_numeric(s, errors="coerce").astype(dtype)
        if dtype == "string":
            return s.astype("string")
        if dtype == "str":  # pandas >= 3 default string dtype (schema stored as "str")
            return s.astype("str")
    except (TypeError, ValueError):
        return s
    return s.astype(object).where(pd.notna(s), None)


def read_frame(conn: Connection, table: Table, schema: Sequence[Sequence[str]], where: Sequence, order_by: str = "row_idx") -> pd.DataFrame:
    """Rebuild a frame previously written with ``write_frame`` (``schema`` from ``schema_of``)."""
    if not schema:
        return pd.DataFrame()
    names = [c for c, _ in schema]
    table_cols = set(table.c.keys())
    canon = [c for c in names if c in table_cols]
    q = select(*(table.c[c] for c in canon), table.c.extra_json).where(*where).order_by(table.c[order_by])
    rows = conn.execute(q).all()
    data: dict[str, list] = {c: [] for c in names}
    needs_extra = [c for c in names if c not in table_cols]
    for r in rows:
        for i, c in enumerate(canon):
            data[c].append(r[i])
        if needs_extra:
            ex = json.loads(r[-1]) if r[-1] else {}
            for c in needs_extra:
                data[c].append(ex.get(c))
    df = pd.DataFrame({c: pd.Series(data[c], dtype=object) for c in names})
    for c, dtype in schema:
        df[c] = _restore(df[c], dtype)
    return df.reset_index(drop=True)
