"""EA SPORTS F1 25: 2026 Season Pack UDP decoder (little-endian, packed). Only the packets F1 SETUP LAB needs.

Layouts follow EA's published F1 25: 2026 Season Pack specification. The header and Session History layout match the
2025 format, but this format supports 24 cars and has different Car Setups / Car Telemetry sizes.
"""
from __future__ import annotations

import struct

from f1lab.integrations.game_udp.protocols.f1_25 import (
    CAR_SETUP,
    HEADER,
    HISTORY_HEAD,
    LAP_HISTORY,
    PID_HISTORY,
    PID_SETUPS,
    PID_TELEMETRY,
    SETUP_FIELDS,
    Header,
    History,
    LapRecord,
    PacketError,
)

CAR_TELEMETRY = struct.Struct("<H3fBbHBBH4H4B4BB4f4B")  # 59 bytes per car
MAX_CARS = 24
EXPECTED_SIZE = {PID_SETUPS: 1233, PID_TELEMETRY: 1448, PID_HISTORY: 1460}
assert (CAR_SETUP.size, CAR_TELEMETRY.size, LAP_HISTORY.size) == (50, 59, 14)
assert len(SETUP_FIELDS) == len(CAR_SETUP.unpack(bytes(50)))


def _check(h: Header, data: bytes) -> None:
    want = EXPECTED_SIZE.get(h.packet_id)
    if want and len(data) != want:
        raise PacketError(f"packet {h.packet_id} is {len(data)} bytes, expected {want}: wrong UDP format selected in the game?")
    if h.player_car >= MAX_CARS:
        raise PacketError(f"invalid player car index {h.player_car}")


def decode_setup(data: bytes, h: Header) -> dict[str, float]:
    _check(h, data)
    return dict(zip(SETUP_FIELDS, CAR_SETUP.unpack_from(data, HEADER.size + h.player_car * CAR_SETUP.size), strict=True))


def decode_telemetry(data: bytes, h: Header) -> dict[str, object]:
    _check(h, data)
    v = CAR_TELEMETRY.unpack_from(data, HEADER.size + h.player_car * CAR_TELEMETRY.size)
    return {
        "speed_kph": v[0],
        "throttle": v[1],
        "steer": v[2],
        "brake": v[3],
        "clutch": v[4],
        "gear": v[5],
        "rpm": v[6],
        "drs": v[7],
        "rev_lights_percent": v[8],
        "rev_lights_bit_value": v[9],
        "brake_temperature_c": v[10:14],
        "tyre_surface_temperature_c": v[14:18],
        "tyre_inner_temperature_c": v[18:22],
        "engine_temperature_c": v[22],
        "tyre_pressure_psi": v[23:27],
        "surface_type": v[27:31],
    }


def decode_history(data: bytes, h: Header) -> History | None:
    """Session history for ONE car (the packet names it). Returns None if it is not the player's car."""
    _check(h, data)
    car, n, _stints, best, *_ = HISTORY_HEAD.unpack_from(data, HEADER.size)
    if car != h.player_car:
        return None
    out = History(car, best_lap=best)
    base = HEADER.size + HISTORY_HEAD.size
    for i in range(min(n, 100)):
        ms, s1, s1m, s2, s2m, s3, s3m, flags = LAP_HISTORY.unpack_from(data, base + i * LAP_HISTORY.size)
        out.laps.append(LapRecord(i + 1, ms / 1000, ((s1m * 60000 + s1) / 1000, (s2m * 60000 + s2) / 1000, (s3m * 60000 + s3) / 1000),
                                  bool(flags & 1), (bool(flags & 2), bool(flags & 4), bool(flags & 8))))
    return out
