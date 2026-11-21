"""Live-simulator mode: spec, guards against the synthetic simulator, UDP capture over a real loopback socket, stint acceptance and rejection."""

from __future__ import annotations

import socket
import time

import pytest
from udp_packets import history_packet, setup_packet, stint_laps, telemetry_packet

FOUR = {
    "front_wing": {"values": [20, 25, 30], "baseline": 25},
    "rear_wing": {"values": [24, 29, 34], "baseline": 29},
    "on_throttle_diff": {"values": [40, 50, 60], "baseline": 50},
    "rear_anti_roll_bar": {"values": [8, 11, 14], "baseline": 11},
}


def live_body(**over):
    body = {"name": "live test", "mode": "live_simulator",
            "live": {"factors": {k: dict(v) for k, v in FOUR.items()}, "min_valid_laps": 3, "max_lap_std": 1.5},
            "design": {"method": "fractional_factorial", "replicates": 1, "run_budget": 40}}
    body.update(over)
    return body


@pytest.fixture
def make_live(client):
    def _make(**over):
        r = client.post("/api/experiments", json=live_body(**over))
        assert r.status_code == 201, r.text
        return r.json()
    return _make


class Game:
    """Plays the part of the game: sends UDP packets to the receiver and waits until they have been processed."""

    def __init__(self, client):
        self.client, self.sent, self.laps = client, 0, []
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def connect(self):
        r = self.client.post("/api/live/connect", json={"host": "127.0.0.1", "port": self.port})
        assert r.status_code == 200, r.text
        return r.json()

    def send(self, *packets):
        for p in packets:
            self.tx.sendto(p, ("127.0.0.1", self.port))
        self.sent += len(packets)
        end = time.time() + 5
        while time.time() < end:
            st = self.client.get("/api/live/status").json()
            if st["packets"] + st["malformed"] + st["wrong_format"] >= self.sent:
                return st
            time.sleep(0.02)
        raise AssertionError("receiver did not process the packets")

    def drive(self, run, exp_id, mean=90.0, setup_over=None):
        """One stint on ``run``: start, set the targeted setup in 'the game', drive five laps, submit."""
        cfg = {k: v for k, v in run["configuration"].items()}
        laps = stint_laps(mean)
        self.laps.append(laps[0])  # the in-lap is driven before the timed stint starts
        self.send(history_packet(self.laps))
        assert self.client.post(f"/api/experiments/{exp_id}/runs/{run['run_id']}/stint/start").status_code == 200
        self.laps += laps[1:]  # the game's history packet is cumulative: every stint appends laps
        self.send(setup_packet(**{**cfg, **(setup_over or {})}), telemetry_packet(305), telemetry_packet(318), history_packet(self.laps))
        return self.client.post(f"/api/experiments/{exp_id}/runs/{run['run_id']}/stint/submit")


@pytest.fixture
def game(client):
    g = Game(client)
    g.connect()
    g.laps = stint_laps(90.0)[:1]
    g.send(setup_packet(), history_packet(g.laps))  # baseline setup + one earlier lap
    yield g
    client.post("/api/live/disconnect")


# ------------------------------------------------------------------ the experiment definition
def test_live_experiment_uses_discrete_game_factors_and_no_synthetic_machinery(client, make_live):
    d = make_live()
    assert d["mode"] == "live_simulator" and d["source_label"] == "SIM-RECORDED" and "SIMULATOR-RECORDED" in d["notice"] and d["catalog_verified"] is False
    assert {f["name"] for f in d["factors"]} == set(FOUR) and all(f["values"] for f in d["factors"]) and d["runs_total"] >= 8 and d["status"] == "designed"
    for r in d["runs"]:
        for f in d["factors"]:
            assert r["configuration"][f["name"]] in f["values"]  # only values the game can actually apply
    assert d["counts"]["completed"] == 0 and not any("stability" in k for k in d["runs"][0])
    lab = client.app.state.lab
    assert not [n for n in lab.registry.names() if d["id"] in n]  # no simulator adapter was registered


@pytest.mark.parametrize(
    "mutate,fragment",
    [
        (lambda b: b["design"].update(method="central_composite"), "needs continuous factors"),
        (lambda b: b["design"].update(method="box_behnken"), "needs continuous factors"),
        (lambda b: b["live"]["factors"]["front_wing"].update(baseline=99), "baseline must be one of"),
        (lambda b: b["live"]["factors"].update(wheel={"values": [1, 2], "baseline": 1}), "unknown game factor"),
        (lambda b: b["live"]["factors"]["front_wing"].update(values=[20]), "at least 2"),
        (lambda b: b["live"]["factors"]["front_wing"].update(values=[20, 20, 25]), "distinct"),
        (lambda b: b["live"].pop("factors") and None, "Field required"),
        (lambda b: b.pop("live"), "need 'live' settings"),
        (lambda b: b.update(mode="synthetic"), "only valid when mode is live_simulator"),
    ],
)
def test_invalid_live_definitions_are_rejected(client, mutate, fragment):
    body = live_body()
    mutate(body)
    r = client.post("/api/experiments", json=body)
    assert r.status_code == 422, r.text
    assert any(fragment in i["message"] for i in r.json()["detail"]), r.text
    assert client.get("/api/experiments").json() == []


def test_synthetic_experiments_are_unchanged(client):
    r = client.post("/api/experiments", json={"name": "synthetic still works", "simulator": {"latency_seconds": 0}})
    assert r.status_code == 201 and r.json()["mode"] == "synthetic" and r.json()["source_label"] == "SYNTHETIC" and "SYNTHETIC DATA" in r.json()["notice"]


# ------------------------------------------------------------------ the synthetic simulator can never touch a live experiment
def test_live_experiment_cannot_be_executed_even_when_a_synthetic_adapter_is_registered(client, make_live):
    client.post("/api/experiments", json={"name": "synthetic first", "simulator": {"latency_seconds": 0}})  # registers the only adapter
    d = make_live()
    for path in ("execute", "retry"):
        r = client.post(f"/api/experiments/{d['id']}/{path}")
        assert r.status_code == 409 and "measured in the simulator" in r.json()["detail"][0]["message"]
    time.sleep(0.3)
    after = client.get(f"/api/experiments/{d['id']}").json()
    assert after["counts"]["completed"] == 0 and after["counts"]["running"] == 0 and after["counts"]["failed"] == 0


def test_csv_import_cannot_be_attached_to_a_live_run(client, make_live):
    d = make_live()
    r = client.post(f"/api/experiments/{d['id']}/runs/{d['runs'][0]['run_id']}/telemetry", json={"csv": "lap,sector_1,sector_2,sector_3\n1,28,35,27\n"})
    assert r.status_code == 409 and "cannot prove which setup" in r.json()["detail"][0]["message"]


# ------------------------------------------------------------------ live capture
def test_status_reports_connection_wrong_format_and_malformed_packets(client):
    g = Game(client)
    st0 = g.connect()
    assert st0["listening"] is True and st0["accepted_formats"] == [2025, 2026]
    st = g.send(setup_packet(), setup_packet(fmt=2023), b"junk")
    assert st["packets"] == 1 and st["wrong_format"] == 1 and st["malformed"] == 1 and st["connected"] is True and st["current_setup"]["front_wing"] == 25
    assert "2026 Season Pack" in st["format_help"]
    assert client.post("/api/live/connect", json={"port": g.port}).status_code == 409  # already listening
    assert client.post("/api/live/disconnect").json()["listening"] is False


def test_stint_cannot_start_without_a_connection_or_data(client, make_live):
    d = make_live()
    run = d["runs"][0]["run_id"]
    assert client.post(f"/api/experiments/{d['id']}/runs/{run}/stint/start").status_code == 409  # not listening
    g = Game(client)
    g.connect()
    r = client.post(f"/api/experiments/{d['id']}/runs/{run}/stint/start")
    assert r.status_code == 409 and "no setup/lap data" in r.json()["detail"][0]["message"]
    client.post("/api/live/disconnect")


def test_accepted_stint_completes_the_run_with_verified_evidence(client, make_live, game):
    d = make_live()
    run = d["runs"][0]
    r = game.drive(run, d["id"], mean=90.0)
    assert r.status_code == 200, r.text
    row = r.json()["run"]
    assert row["status"] == "completed" and row["imported"] is True
    assert row["outcomes"]["lap_time"] == pytest.approx(90.0, abs=0.01) and row["outcomes"]["valid_laps"] == 3 and row["outcomes"]["lap_time_std"] == pytest.approx(0.1, abs=0.01)
    assert row["outcomes"]["top_speed"] == pytest.approx(318) and row["feasible"] is True
    ev = client.get(f"/api/experiments/{d['id']}/runs/{run['run_id']}/evidence").json()
    assert ev["accepted"] is True and ev["target"] == run["configuration"] and len(ev["laps"]) == 3
    assert client.post(f"/api/experiments/{d['id']}/runs/{run['run_id']}/stint/submit").status_code == 409  # nothing in progress
    assert client.post(f"/api/experiments/{d['id']}/runs/{run['run_id']}/stint/start").status_code == 409  # a completed run is final


@pytest.mark.parametrize(
    "over,fragment",
    [
        ({"front_wing": 40}, "does not match the target for front_wing"),   # the driver set the wrong wing
        ({"brake_bias": 60}, "outside the experiment"),                     # an untouched field was changed too
    ],
)
def test_stint_that_does_not_prove_the_target_setup_is_rejected_and_run_stays_queued(client, make_live, game, over, fragment):
    d = make_live()
    run = d["runs"][0]
    r = game.drive(run, d["id"], setup_over=over)
    assert r.status_code == 409 and fragment in r.json()["detail"][0]["message"]
    after = client.get(f"/api/experiments/{d['id']}").json()
    assert after["runs"][0]["status"] in ("queued", "created") and after["counts"]["completed"] == 0
    ev = client.get(f"/api/experiments/{d['id']}/runs/{run['run_id']}/evidence").json()
    assert ev["accepted"] is False and fragment in " ".join(ev["reasons"])
    assert game.drive(run, d["id"]).status_code == 200  # the same run can be driven again correctly


def test_inconsistent_stint_violates_the_spread_constraint(client, make_live, game):
    d = make_live(live={"factors": FOUR, "min_valid_laps": 3, "max_lap_std": 0.05})
    r = game.drive(d["runs"][0], d["id"])
    assert r.status_code == 200 and r.json()["run"]["feasible"] is False  # lap spread 0.1 s > 0.05 s


def test_full_live_experiment_analysis_and_report_are_labelled_as_sim_recorded(client, make_live, game):
    d = make_live()
    for i, run in enumerate(d["runs"]):
        mean = 90.0 - 0.02 * run["configuration"]["front_wing"] + 0.01 * run["configuration"]["rear_wing"] + 0.001 * i
        assert game.drive(run, d["id"], mean=mean).status_code == 200
    final = client.get(f"/api/experiments/{d['id']}").json()
    assert final["status"] == "completed" and final["counts"]["completed"] == final["runs_total"]
    a = client.post(f"/api/experiments/{d['id']}/analysis", json={}).json()
    rec = a["recommendation"]
    assert rec["status"] == "recommended" and "spread" in rec["headline"] and "stability" not in rec["headline"].lower()
    caveats = " ".join(rec["caveats"])
    assert "SIMULATOR-RECORDED" in caveats and "not been verified" in caveats and "one driver in one game" in caveats
    fastest = min(final["runs"], key=lambda r: r["outcomes"]["lap_time"])
    assert a["best_run_id"] == fastest["run_id"]
    html = client.get(f"/api/experiments/{d['id']}/report.html").text
    assert "SIMULATOR-RECORDED DATA" in html and "lap spread" in html and "SYNTHETIC DATA" not in html


def test_live_runs_and_evidence_survive_a_restart(db_path, make_live, game, client):
    from fastapi.testclient import TestClient

    from f1lab.api.app import create_app

    d = make_live()
    assert game.drive(d["runs"][0], d["id"]).status_code == 200
    with TestClient(create_app(db_path)) as c2:
        again = c2.get(f"/api/experiments/{d['id']}").json()
        assert again["counts"]["completed"] == 1 and again["mode"] == "live_simulator" and again["runs"][0]["outcomes"]["valid_laps"] == 3
        assert c2.get(f"/api/experiments/{d['id']}/runs/{d['runs'][0]['run_id']}/evidence").json()["accepted"] is True
        assert c2.post(f"/api/experiments/{d['id']}/execute").status_code == 409  # still guarded after recovery
