"""Property tests for season comparison shape (Requirements 4.2, 4.3, design D9)."""
from __future__ import annotations
from hypothesis import given, settings, strategies as st
from f1lab.integrations.historical.analysis import COMPARE_METRICS, season_compare

CODES = ["VER", "HAM", "LEC", "NOR", "ALO", "RUS"]
_opt_num = st.one_of(st.none(), st.integers(0, 30))


@st.composite
def _session(draw, idx: int):
    members = draw(st.lists(st.sampled_from(CODES), unique=True, max_size=6))
    laps = [
        {
            "driver": draw(st.sampled_from(CODES)),
            "lap_time_s": draw(st.one_of(st.none(), st.floats(60, 120))),
            "is_valid": draw(st.booleans()),
            "is_deleted": draw(st.booleans()),
            "pits": draw(st.booleans()),
        }
        for _ in range(draw(st.integers(0, 10)))
    ]
    datasets = {}
    for name in ("session_result", "starting_grid", "drivers_championship"):
        if draw(st.booleans()):
            datasets[name] = None
            continue
        rows = []
        for code in draw(st.lists(st.sampled_from(CODES), unique=True, max_size=6)):
            rows.append({
                "driver": code,
                "position": draw(_opt_num),
                "points_start": draw(_opt_num),
                "points_current": draw(_opt_num),
            })
        datasets[name] = rows
    return {
        "id": f"s{idx}",
        "year": 2024,
        "event": f"E{idx}",
        "session_type": "Race",
        "date_start": draw(st.one_of(st.none(), st.integers(0, 5).map(lambda d: f"2024-01-0{d + 1}T10:00:00"))),
        "drivers": [{"code": c, "number": i + 1} for i, c in enumerate(members)],
        "laps": laps,
        "datasets": datasets,
    }


@st.composite
def _case(draw):
    n = draw(st.integers(0, 6))
    sessions = [draw(_session(i)) for i in range(n)]
    drivers = draw(st.lists(st.sampled_from(CODES), unique=True, max_size=6))
    return sessions, drivers, draw(st.sampled_from(COMPARE_METRICS))


# Feature: f1-historical-phase1, Property 8: Season comparison shape
@settings(max_examples=200, deadline=None)
@given(_case())
def test_property_8_season_comparison_shape(case):
    """**Validates: Requirements 4.2, 4.3**"""
    sessions, drivers, metric = case
    out = season_compare(sessions, drivers, metric)

    # sessions in date order (undated sessions sort first, ties keep input order)
    keys = [str(s.get("date_start") or "") for s in out["sessions"]]
    assert keys == sorted(keys)
    assert sorted(s["id"] for s in out["sessions"]) == sorted(s["id"] for s in sessions)

    # exactly one cell per driver per session, grouped in session order
    expected = [(s["id"], d) for s in out["sessions"] for d in drivers]
    assert [(c["session_id"], c["driver"]) for c in out["cells"]] == expected

    # a value or a reason, never both and never neither
    for c in out["cells"]:
        has_value = c["value"] is not None
        has_reason = bool(c["reason"])
        assert has_value != has_reason
