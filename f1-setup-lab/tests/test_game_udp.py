"""Tests for the UDP decoder, receiver, raw recorder and stint verification. No game needed: packets are synthesised."""
from __future__ import annotations

import asyncio
import socket
import struct

import pytest
from f1lab.integrations.game_udp import recorder as rec
from f1lab.integrations.game_udp.capture import StintRecorder, same
from f1lab.integrations.game_udp.live import LiveState
from f1lab.integrations.game_udp.protocols import f1_25 as P
from f1lab.integrations.game_udp.receiver import UdpReceiver

BASE = dict(front_wing=25, rear_wing=29, on_throttle_diff=50, off_throttle_diff=50, front_camber=-3.0, rear_camber=-1.5, front_toe=0.05, rear_toe=0.20,
            front_suspension=21, rear_suspension=21, front_anti_roll_bar=11, rear_anti_roll_bar=11, front_ride_height=20, rear_ride_height=40,
            brake_pressure=100, brake_bias=55, engine_braking=50, rear_left_tyre_psi=22.5, rear_right_tyre_psi=22.5, front_left_tyre_psi=23.0,
            front_right_tyre_psi=23.0, ballast=6, fuel_load=30.0)
LAPS = [(90.5, (28.1, 35.2, 27.2), 15), (89.4, (27.9, 34.5, 27.0), 15), (89.7, (28.0, 34.6, 27.1), 15), (95.0, (28.0, 40.0, 27.0), 14),
        (89.2, (27.8, 34.4, 27.0), 15), (89.0, (27.7, 34.3, 27.0), 15)]


def header(pid, player=3, fmt=2025):
    return P.HEADER.pack(fmt, 25, 1, 0, 1, pid, 777, 100.0, 1000, 1000, player, 255)


def setup_packet(player=3, fmt=2025, **over):
    s = {**BASE, **over}
    cars = [P.CAR_SETUP.pack(*[s[k] for k in P.SETUP_FIELDS]) if i == player else bytes(50) for i in range(22)]
    return header(5, player, fmt) + b"".join(cars) + struct.pack("<f", 0.0)


def history_packet(n_laps, player=3, car=None):
    car = player if car is None else car
    rows = b""
    for t, s, flags in LAPS[:n_laps]:
        rows += P.LAP_HISTORY.pack(int(t * 1000), int(s[0] * 1000) % 60000, int(s[0] // 60), int(s[1] * 1000) % 60000, int(s[1] // 60), int(s[2] * 1000) % 60000, int(s[2] // 60), flags)
    return header(11, player) + P.HISTORY_HEAD.pack(car, n_laps, 1, 1, 1, 1, 1) + rows + bytes(14 * (100 - n_laps)) + bytes(24)


def telemetry_packet(speed, player=3):
    one = P.CAR_TELEMETRY.pack(speed, .9, 0.0, 0.0, 0, 7, 11000, 0, 50, 0, 0, 0, 0, 0, 90, 90, 90, 90, 100, 100, 100, 100, 100, 23, 23, 23, 23, 0, 0, 0, 0)
    return header(6, player) + b"".join(one if i == player else bytes(60) for i in range(22)) + bytes(3)


def feed(live, *packets):
    for p in packets:
        live.on_packet(P.parse_header(p), p)


# ------------------------------------------------------------------ decoder
def test_packet_sizes_match_the_published_specification():
    assert len(setup_packet()) == 1133 and len(history_packet(3)) == 1460 and len(telemetry_packet(300)) == 1352 and P.HEADER.size == 29


def test_setup_decoding_returns_the_players_car_only():
    d = setup_packet(player=7, front_wing=31, front_camber=-2.5)
    s = P.decode_setup(d, P.parse_header(d))
    assert s["front_wing"] == 31 and s["front_camber"] == pytest.approx(-2.5) and s["fuel_load"] == pytest.approx(30.0) and set(s) == set(P.SETUP_FIELDS)


def test_history_decoding_and_other_cars_are_ignored():
    d = history_packet(5)
    h = P.decode_history(d, P.parse_header(d))
    assert len(h.laps) == 5 and h.laps[1].lap_time_s == pytest.approx(89.4) and h.laps[3].valid is False and h.laps[0].sectors_s[1] == pytest.approx(35.2)
    other = history_packet(5, car=5)
    assert P.decode_history(other, P.parse_header(other)) is None


@pytest.mark.parametrize("bad", [b"", b"\x00" * 10, setup_packet()[:-1], setup_packet()[:1100], setup_packet() + b"x"])
def test_malformed_packets_raise_packet_error_not_crash(bad):
    with pytest.raises(P.PacketError):
        P.decode_setup(bad, P.parse_header(bad))


# ------------------------------------------------------------------ stint verification
def stint(setups, n_start=1, n_end=6, target=None, **kw):
    target = target or {"front_wing": 30, "rear_wing": 32}
    r = StintRecorder(target, BASE, **kw)
    h = lambda n: P.decode_history(history_packet(n), P.parse_header(history_packet(n)))  # noqa: E731
    r.on_history(h(n_start))
    r.start()
    for s in setups:
        r.on_setup(s)
    r.on_history(h(n_end))
    return r.stop()


GOOD = {**BASE, "front_wing": 30, "rear_wing": 32}


def test_matching_stint_is_accepted_and_out_lap_and_invalid_laps_are_dropped():
    r = stint([GOOD, GOOD], min_valid_laps=3)
    assert r.accepted and [x.lap for x in r.laps] == [3, 5, 6] and r.reasons == []  # lap 2 = out-lap, lap 4 invalid


@pytest.mark.parametrize(
    "setups,fragment",
    [
        ([{**BASE, "front_wing": 25, "rear_wing": 32}], "does not match the target"),
        ([GOOD, {**GOOD, "rear_wing": 40}], "changed during the stint"),
        ([{**GOOD, "brake_bias": 60}], "outside the experiment"),
        ([], "no setup packet"),
    ],
)
def test_stints_that_do_not_prove_the_target_setup_are_rejected(setups, fragment):
    r = stint(setups)
    assert not r.accepted and any(fragment in x for x in r.reasons)


def test_too_few_valid_laps_rejects():
    assert any("valid timed laps" in x for x in stint([GOOD], n_end=3).reasons)


def test_tolerances_integers_exact_floats_close():
    assert same({"front_camber": -3.0}, {"front_camber": -3.004}) == [] and same({"front_wing": 25}, {"front_wing": 26}) == ["front_wing"]


# ------------------------------------------------------------------ live state end to end
def test_live_state_records_a_stint_and_top_speed():
    live = LiveState()
    with pytest.raises(RuntimeError):
        live.start_stint({"front_wing": 30})  # nothing received yet
    feed(live, setup_packet(), history_packet(1))
    live.start_stint({"front_wing": 30, "rear_wing": 32})
    feed(live, setup_packet(front_wing=30, rear_wing=32), telemetry_packet(301), telemetry_packet(318), history_packet(6))
    result, top = live.stop_stint()
    assert result.accepted and top == pytest.approx(318) and len(result.laps) == 3


# ------------------------------------------------------------------ raw recording and the receiver
def test_raw_recording_round_trips_and_tolerates_a_truncated_tail(tmp_path):
    p = tmp_path / "s.f1raw"
    r = rec.RawRecorder(p)
    r.write(b"abc", 1.0)
    r.write(setup_packet(), 2.0)
    r.close()
    with p.open("ab") as f:
        f.write(b"\x00\x00\x00")  # crash mid-write
    got = list(rec.read_raw(p))
    assert [t for t, _ in got] == [1.0, 2.0] and got[0][1] == b"abc" and len(got[1][1]) == 1133


def test_receiver_over_real_udp_counts_good_bad_and_wrong_format(tmp_path):
    seen = []

    def on_packet(h, data):
        seen.append(h.packet_id)
        if h.packet_id == P.PID_SETUPS:
            P.decode_setup(data, h)

    async def main():
        rx = UdpReceiver(on_packet, rec.RawRecorder(tmp_path / "raw.f1raw"))
        port = await rx.start("127.0.0.1", 0)
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for pkt in (setup_packet(), history_packet(2), b"junk", setup_packet(fmt=2023), setup_packet()[:-5]):
            tx.sendto(pkt, ("127.0.0.1", port))
        await asyncio.sleep(0.3)
        st = rx.status()
        rx.stop()
        return st

    st = asyncio.run(main())
    assert seen == [5, 11, 5] and st["packets"] == 3 + 0 and st["malformed"] == 2 and st["wrong_format"] == 1 and st["connected"] is True
    assert len(list(rec.read_raw(tmp_path / "raw.f1raw"))) == 3
