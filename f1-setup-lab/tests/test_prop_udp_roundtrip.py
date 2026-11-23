"""Property test: recorded UDP packets round-trip through the database recorder and the legacy .f1raw file format."""
from __future__ import annotations
import tempfile
import uuid
from pathlib import Path
import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from f1lab.integrations.game_udp import recorder as rec

# RawRecorder.write treats a falsy receive time as "now", so timestamps are strictly positive.
_PACKETS = st.lists(
    st.tuples(st.floats(min_value=1.0, max_value=4e9, allow_nan=False, allow_infinity=False), st.binary(max_size=64)),
    max_size=30,
)


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    from db import create_db_engine, migrate
    e = create_db_engine(tmp_path_factory.mktemp("udp") / "trialforge.db")
    migrate(e)
    return e


# Feature: f1-setup-lab-restoration, Property 15: Recorded UDP packets round-trip
# **Validates: Requirements 9.2**
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(packets=_PACKETS)
def test_recorded_udp_packets_round_trip(engine, packets):
    rid = "prop-" + uuid.uuid4().hex
    r = rec.RawRecorder(engine, rid)
    for t, data in packets:
        r.write(data, t)
    r.close()
    assert list(rec.read_raw(engine, rid)) == packets

    with tempfile.TemporaryDirectory() as d:
        path = rec.write_raw_file(Path(d) / "raw.f1raw", packets)
        assert list(rec.read_raw_file(path)) == packets
