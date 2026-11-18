"""Tests for the EA SPORTS F1 25: 2026 Season Pack decoder, protocol dispatch, telemetry buffer and session hardening."""
from __future__ import annotations

import struct

import pytest
from f1lab.integrations.game_udp import protocols as REG
from f1lab.integrations.game_udp.live import LiveState
from f1lab.integrations.game_udp.protocols import f1_25_2026 as P
from f1lab.integrations.game_udp.receiver import UdpReceiver
from udp_packets import BASE_SETUP

LAPS = [(90.5, (28.1, 35.2, 27.2), 15), (89.4, (27.9, 34.5, 27.0), 15), (89.7, (28.0, 34.6, 27.1), 15), (89.2, (27.8, 34.4, 27.0), 15), (89.0, (27.7, 34.3, 27.0), 15)]


def header(pid: int, player: int = 3, fmt: int = 2026, session_uid: int = 888) -> bytes:
    return P.HEADER.pack(fmt, 25, 1, 0, 1, pid, session_uid, 100.0, 1000, 1000, player, 255)


def setup_packet(player: int = 3, fmt: int = 2026, session_uid: int = 888, **over) -> bytes:
    s = {**BASE_SETUP, **over}
    cars = [P.CAR_SETUP.pack(*[s[k] for k in P.SETUP_FIELDS]) if i == player else bytes(50) for i in range(24)]
    return header(5, player, fmt, session_uid) + b"".join(cars) + struct.pack("<f", 0.0)


def telemetry_packet(speed: int = 300, player: int = 3, session_uid: int = 888) -> bytes:
    one = P.CAR_TELEMETRY.pack(speed, .9, 0.0, 0.0, 0, 7, 11000, 0, 50, 0,
                               90, 90, 90, 90, 25, 25, 25, 25, 80, 80, 80, 80, 90,
                               23.0, 23.0, 23.0, 23.0, 0, 0, 0, 0)
    return header(6, player, 2026, session_uid) + b"".join(one if i == player else bytes(59) for i in range(24)) + bytes(3)


def history_packet(n_laps: int, player: int = 3, session_uid: int = 888) -> bytes:
    rows = b""
    for t, s, flags in LAPS[:n_laps]:
        rows += P.LAP_HISTORY.pack(int(t * 1000), int(s[0] * 1000) % 60000, int(s[0] // 60), int(s[1] * 1000) % 60000, int(s[1] // 60),
                                   int(s[2] * 1000) % 60000, int(s[2] // 60), flags)
    return header(11, player, 2026, session_uid) + P.HISTORY_HEAD.pack(player, n_laps, 1, 1, 1, 1, 1) + rows + bytes(14 * (100 - n_laps)) + bytes(24)


def feed(live: LiveState, *packets: bytes) -> None:
    for packet in packets:
        live.on_packet(REG.parse_header(packet), packet)


# ------------------------------------------------------------------ decoder and registry
def test_2026_packet_sizes_match_the_season_pack_specification():
    assert len(setup_packet()) == 1233 and len(telemetry_packet()) == 1448 and len(history_packet(3)) == 1460
    assert REG.accepted_formats() == {2025, 2026}


def test_2026_decoders_return_setup_telemetry_and_history():
    setup = P.decode_setup(setup_packet(front_wing=31), REG.parse_header(setup_packet(front_wing=31)))
    assert setup["front_wing"] == 31 and setup["fuel_load"] == pytest.approx(30.0) and set(setup) == set(P.SETUP_FIELDS)

    tel = P.decode_telemetry(telemetry_packet(318), REG.parse_header(telemetry_packet(318)))
    assert tel["speed_kph"] == 318 and tel["throttle"] == pytest.approx(0.9) and tel["gear"] == 7
    assert tel["tyre_pressure_psi"] == pytest.approx((23.0, 23.0, 23.0, 23.0)) and tel["engine_temperature_c"] == 90

    hist = P.decode_history(history_packet(4), REG.parse_header(history_packet(4)))
    assert hist is not None and len(hist.laps) == 4 and hist.laps[1].lap_time_s == pytest.approx(89.4)


def test_receiver_dispatches_2026_packets_through_the_registry():
    seen = []
    rx = UdpReceiver(lambda h, data: seen.append((h.packet_format, h.packet_id)))
    rx.datagram_received(setup_packet(), None)
    rx.datagram_received(telemetry_packet(), None)
    rx.datagram_received(history_packet(1), None)
    assert seen == [(2026, 5), (2026, 6), (2026, 11)]
    assert rx.status()["packets"] == 3 and rx.status()["wrong_format"] == 0


# ------------------------------------------------------------------ live state, telemetry buffer and session hardening
def test_live_state_records_a_2026_stint_and_buffers_telemetry():
    live = LiveState()
    feed(live, setup_packet(), history_packet(1), telemetry_packet(305))
    live.start_stint({"front_wing": 30, "rear_wing": 32})
    feed(live, setup_packet(front_wing=30, rear_wing=32), telemetry_packet(301), telemetry_packet(318), history_packet(5))
    result, top = live.stop_stint()
    assert result.accepted and top == pytest.approx(318) and len(result.laps) == 3
    assert live.session_uid == 888 and live.packet_format == 2026
    assert live.stint_telemetry is None and live.last_stint_telemetry is not None
    summary = live.last_stint_telemetry.summary()
    assert summary["samples"] == 2 and summary["top_speed_kph"] == pytest.approx(318) and summary["packet_formats"] == [2026]
    assert live.telemetry.summary()["samples"] >= 3


def test_session_change_during_a_stint_rejects_it_and_resets_state():
    live = LiveState()
    feed(live, setup_packet(), history_packet(1))
    live.start_stint({"front_wing": 30, "rear_wing": 32})
    feed(live, setup_packet(front_wing=30, rear_wing=32, session_uid=999), history_packet(4, session_uid=999))
    result, _ = live.stop_stint()
    assert not result.accepted and "game session changed" in " ".join(result.reasons)
    assert live.session_uid == 999 and live.packet_format == 2026 and live.baseline == live.setup and len(live.history.laps) == 4
