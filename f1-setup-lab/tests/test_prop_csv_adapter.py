"""Task 6.4: property tests for the Setup Engineering telemetry adapter (f1-unified-telemetry-import, Properties 29-31)."""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import tempfile
from datetime import datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from f1lab.adapters.telemetry import UNAVAILABLE_TIMESTAMP, parse_telemetry_csv, to_measurements
from f1lab.api.schemas import ExperimentRequest
from f1lab.integrations.historical import csv_normalizer as norm
from f1lab.services.lab import LabService, MissingSourceField, source_facts

SETTINGS = settings(max_examples=100, deadline=None)
_counter = itertools.count()
DERIVED = {"speed_kph", "rpm", "brake_pct", "stability"}


# ---- local strategies -------------------------------------------------------------------------------------------

sector_cell = st.integers(1, 90000).map(lambda k: f"{k / 1000:.3f}")
timestamp_cell = st.one_of(
    st.just(""),
    st.datetimes(min_value=datetime(2020, 1, 1), max_value=datetime(2030, 1, 1)).map(lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")),
)


@st.composite
def lap_csv(draw, with_lap_time=None, with_timestamp=None, with_extras=None):
    """CSV text of a valid lap table: lap + 3 sectors, optionally lap_time, timestamp (cells may be empty), stability/top_speed."""
    n = draw(st.integers(1, 8))
    flags = [draw(st.booleans()) if f is None else f for f in (with_lap_time, with_timestamp, with_extras)]
    cols: dict[str, list[str]] = {"lap": [str(i + 1) for i in range(n)]}
    for s in ("sector_1", "sector_2", "sector_3"):
        cols[s] = draw(st.lists(sector_cell, min_size=n, max_size=n))
    if flags[0]:
        cols["lap_time"] = draw(st.lists(sector_cell, min_size=n, max_size=n))
    if flags[1]:
        cols["timestamp"] = draw(st.lists(timestamp_cell, min_size=n, max_size=n))
    if flags[2]:
        cols["stability"] = draw(st.lists(sector_cell, min_size=n, max_size=n))
        cols["top_speed"] = draw(st.lists(sector_cell, min_size=n, max_size=n))
    names = draw(st.permutations(list(cols)))
    return "\n".join([",".join(names)] + [",".join(cols[c][i] for c in names) for i in range(n)]) + "\n"


filenames = st.from_regex(r"[a-z][a-z0-9_]{0,12}\.csv", fullmatch=True)
time_units = st.sampled_from(["s", "ms"])


# ---- Property 29 ------------------------------------------------------------------------------------------------

class Env:
    """One LabService; ``fresh_run`` makes a new experiment (a run is completed, and so frozen, by its first ingest)."""

    def __init__(self, lab, loop):
        self.lab, self.loop = lab, loop

    def fresh_run(self):
        exp_id = f"adapter-props-{next(_counter)}"
        self.lab.create(ExperimentRequest(name="adapter props", experiment_id=exp_id, simulator={"latency_seconds": 0.0}))
        return exp_id, self.lab.runs(exp_id)[0]["run_id"]

    def run(self, exp_id, run_id):
        return next(r for r in self.lab._result(exp_id).runs if r.run_id == run_id)


@pytest.fixture(scope="module")
def env():
    with tempfile.TemporaryDirectory() as d:
        loop = asyncio.new_event_loop()
        yield Env(LabService(f"{d}/trialforge.db"), loop)
        loop.close()


# Feature: f1-unified-telemetry-import, Property 29: Measurement provenance fields round-trip from their source session
# **Validates: Requirements 13.7, 13.8**
@SETTINGS
@given(text=lap_csv(), unit=time_units, name=filenames)
def test_property_29_provenance_round_trip(env, text, unit, name):
    lab, loop = env.lab, env.loop
    exp_id, run_id = env.fresh_run()
    facts = source_facts(text, unit, name)
    assert facts["file_hash"] == hashlib.sha256(text.encode()).hexdigest()
    expected = {k: facts[k] for k in ("original_filename", "file_hash", "column_mapping", "units")}
    assert expected["original_filename"] == name and expected["units"] == {"time": "ms" if unit == "ms" else "seconds"}

    session = lab.import_session(text, unit, name)  # the source session records the same facts
    assert {k: session[k] for k in expected} == expected

    loop.run_until_complete(lab.attach_telemetry(exp_id, run_id, text, unit, name))
    mine = [m for m in env.run(exp_id, run_id).measurements if m.source == f"csv:{name}"]
    assert mine
    for m in mine:
        assert {k: m.metadata.get(k) for k in expected} == expected


# unavailable facts: nothing persisted, error names the field
@SETTINGS
@given(text=lap_csv(), unit=time_units, blank=st.sampled_from(["", "   "]))
def test_property_29_missing_fact_persists_nothing(env, text, unit, blank):
    lab, loop = env.lab, env.loop
    exp_id, run_id = env.fresh_run()
    before = len(env.run(exp_id, run_id).measurements)
    with pytest.raises(MissingSourceField) as ei:
        loop.run_until_complete(lab.attach_telemetry(exp_id, run_id, text, unit, blank))
    assert ei.value.field == "original_filename" and "original_filename" in str(ei.value)
    with pytest.raises(MissingSourceField):
        lab.import_session(text, unit, blank)
    assert len(env.run(exp_id, run_id).measurements) == before


# ---- Property 30 ------------------------------------------------------------------------------------------------

# Feature: f1-unified-telemetry-import, Property 30: Setup Engineering adapter output matches the shared normalizer
# **Validates: Requirements 14.2, 14.3, 14.4**
@SETTINGS
@given(text=lap_csv(), unit=time_units)
def test_property_30_adapter_matches_normalizer(text, unit):
    parsed = parse_telemetry_csv(text, unit)
    header = text.splitlines()[0].split(",")
    mapping = {"lap_number": "lap", "sector_1_s": "sector_1", "sector_2_s": "sector_2", "sector_3_s": "sector_3"}
    if "lap_time" in header:
        mapping["lap_time_s"] = "lap_time"
    ref = norm.parse_csv(text, mapping, {"time": "seconds"}, {"data_type": norm.LAP_SUMMARY})
    assert not ref.blocking_errors and parsed.errors == [] and len(parsed.laps) == len(ref.laps)
    rows = [dict(zip(header, ln.split(","))) for ln in text.splitlines()[1:]]
    for i, rec in enumerate(parsed.laps):
        assert rec["lap"] == int(ref.laps["lap_number"].iloc[i])
        for k in (1, 2, 3):
            assert rec[f"sector_{k}"] == float(ref.laps[f"sector_{k}_s"].iloc[i])
        if "lap_time" in header:
            assert rec["lap_time"] == float(ref.laps["lap_time_s"].iloc[i])
        else:  # missing lap time: adapter's documented sector-sum default
            assert rec["lap_time"] == sum(rec[f"sector_{k}"] for k in (1, 2, 3))
        raw_ts = rows[i].get("timestamp", "")
        if raw_ts:
            assert rec["timestamp"] == norm.parse_timestamp(raw_ts) and rec["timestamp_raw"] == raw_ts
        else:  # no timestamp in the file: unavailable, never computed (no 90 s spacing)
            assert rec["timestamp"] is None and "timestamp_raw" not in rec
    for m in to_measurements("r1", parsed, "csv:x"):
        rec = parsed.laps[m.metadata["lap"] - 1]
        if rec["timestamp"] is None:
            assert m.timestamp == UNAVAILABLE_TIMESTAMP and m.metadata["timestamp_unavailable"] is True
            assert "timestamp_unavailable" in m.quality_flags
        else:
            assert m.timestamp == rec["timestamp"] and "timestamp_unavailable" not in m.quality_flags


# ---- Property 31 ------------------------------------------------------------------------------------------------

# Feature: f1-unified-telemetry-import, Property 31: A sector-only file produces no derived channel measurements
# **Validates: Requirements 14.6**
@SETTINGS
@given(text=lap_csv(with_lap_time=False, with_timestamp=False, with_extras=False), unit=time_units)
def test_property_31_sector_only_has_no_derived_channels(text, unit):
    parsed = parse_telemetry_csv(text, unit)
    assert parsed.laps
    metrics = {m.metric for m in to_measurements("r1", parsed, "csv:x")}
    assert metrics and not (metrics & DERIVED)
    assert metrics == {"sector_1", "sector_2", "sector_3", "lap_time"}
