"""EA SPORTS F1 25 UDP decoder (little-endian, packed). Only the packets F1 SETUP LAB needs.

Layouts follow EA's published F1 25 specification; sizes asserted below match the published packet sizes
(CarSetups 1133, SessionHistory 1460, CarTelemetry 1352). Re-check offsets against the spec revision you target.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

HEADER = struct.Struct("<HBBBBBQfIIBB")          # 29 bytes
assert HEADER.size == 29
PID_SESSION, PID_LAP_DATA, PID_SETUPS, PID_TELEMETRY, PID_HISTORY = 1, 2, 5, 6, 11
CAR_SETUP = struct.Struct("<BBBBffffBBBBBBBBBffffBf")   # 50 bytes per car
CAR_TELEMETRY = struct.Struct("<HfffBbHBBH4H4B4BH4f4B")  # 60 bytes per car
LAP_HISTORY = struct.Struct("<IHBHBHBB")                 # 14 bytes per lap, 100 laps
STINT_HISTORY = struct.Struct("<BBB")                    # 3 bytes, 8 stints
HISTORY_HEAD = struct.Struct("<BBBBBBB")                 # carIdx,numLaps,numTyreStints,bestLap,bestS1,bestS2,bestS3
assert (CAR_SETUP.size, CAR_TELEMETRY.size, LAP_HISTORY.size) == (50, 60, 14)
EXPECTED_SIZE = {PID_SETUPS: 1133, PID_HISTORY: 1460, PID_TELEMETRY: 1352}
SETUP_FIELDS = ("front_wing", "rear_wing", "on_throttle_diff", "off_throttle_diff", "front_camber", "rear_camber", "front_toe", "rear_toe",
                "front_suspension", "rear_suspension", "front_anti_roll_bar", "rear_anti_roll_bar", "front_ride_height", "rear_ride_height",
                "brake_pressure", "brake_bias", "engine_braking", "rear_left_tyre_psi", "rear_right_tyre_psi", "front_left_tyre_psi",
                "front_right_tyre_psi", "ballast", "fuel_load")
assert len(SETUP_FIELDS) == len(CAR_SETUP.unpack(bytes(50)))


class PacketError(ValueError):
    pass


@dataclass(frozen=True)
class Header:
    packet_format: int; game_year: int; game_major: int; game_minor: int; packet_version: int; packet_id: int
    session_uid: int; session_time: float; frame_id: int; overall_frame_id: int; player_car: int; secondary_car: int


def parse_header(data: bytes) -> Header:
    if len(data) < HEADER.size:
        raise PacketError(f"datagram too short ({len(data)} bytes)")
    return Header(*HEADER.unpack_from(data))


def _check(h: Header, data: bytes) -> None:
    want = EXPECTED_SIZE.get(h.packet_id)
    if want and len(data) != want:
        raise PacketError(f"packet {h.packet_id} is {len(data)} bytes, expected {want}: wrong UDP format selected in the game?")
    if h.player_car >= 22:
        raise PacketError(f"invalid player car index {h.player_car}")


def decode_setup(data: bytes, h: Header) -> dict[str, float]:
    _check(h, data)
    return dict(zip(SETUP_FIELDS, CAR_SETUP.unpack_from(data, HEADER.size + h.player_car * CAR_SETUP.size), strict=True))


def decode_telemetry(data: bytes, h: Header) -> dict[str, float]:
    _check(h, data)
    v = CAR_TELEMETRY.unpack_from(data, HEADER.size + h.player_car * CAR_TELEMETRY.size)
    return {"speed_kph": v[0], "throttle": v[1], "steer": v[2], "brake": v[3], "gear": v[5], "rpm": v[6]}


@dataclass
class LapRecord:
    lap: int; lap_time_s: float; sectors_s: tuple[float, float, float]; valid: bool; sectors_valid: tuple[bool, bool, bool]


@dataclass
class History:
    car_index: int; laps: list[LapRecord] = field(default_factory=list); best_lap: int = 0


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
