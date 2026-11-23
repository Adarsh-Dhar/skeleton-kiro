"""Synthetic EA SPORTS F1 25 UDP packets for tests (layout and sizes follow EA's published specification)."""

from __future__ import annotations

import struct

from f1lab.integrations.game_udp.protocols import f1_25 as P

BASE_SETUP = dict(front_wing=25, rear_wing=29, on_throttle_diff=50, off_throttle_diff=50, front_camber=-3.0, rear_camber=-1.5, front_toe=0.05, rear_toe=0.20,
                  front_suspension=21, rear_suspension=21, front_anti_roll_bar=11, rear_anti_roll_bar=11, front_ride_height=20, rear_ride_height=40,
                  brake_pressure=100, brake_bias=55, engine_braking=50, rear_left_tyre_psi=22.5, rear_right_tyre_psi=22.5, front_left_tyre_psi=23.0,
                  front_right_tyre_psi=23.0, ballast=6, fuel_load=30.0)


FLOAT_FIELDS = {"front_camber", "rear_camber", "front_toe", "rear_toe", "rear_left_tyre_psi", "rear_right_tyre_psi", "front_left_tyre_psi", "front_right_tyre_psi", "fuel_load"}


def header(pid: int, player: int = 3, fmt: int = 2025) -> bytes:
    return P.HEADER.pack(fmt, 25, 1, 0, 1, pid, 777, 100.0, 1000, 1000, player, 255)


def setup_packet(player: int = 3, fmt: int = 2025, **over) -> bytes:
    s = {**BASE_SETUP, **over}
    s = {k: (float(v) if k in FLOAT_FIELDS else int(v)) for k, v in s.items()}  # design values arrive as floats; the packet stores integer steps
    cars = [P.CAR_SETUP.pack(*[s[k] for k in P.SETUP_FIELDS]) if i == player else bytes(50) for i in range(22)]
    return header(5, player, fmt) + b"".join(cars) + struct.pack("<f", 0.0)


def history_packet(laps: list[tuple[float, tuple[float, float, float], int]], player: int = 3) -> bytes:
    rows = b""
    for t, s, flags in laps:
        rows += P.LAP_HISTORY.pack(int(t * 1000), int(s[0] * 1000) % 60000, int(s[0] // 60), int(s[1] * 1000) % 60000, int(s[1] // 60), int(s[2] * 1000) % 60000, int(s[2] // 60), flags)
    return header(11, player) + P.HISTORY_HEAD.pack(player, len(laps), 1, 1, 1, 1, 1) + rows + bytes(14 * (100 - len(laps))) + bytes(24)


def telemetry_packet(speed: int, player: int = 3) -> bytes:
    one = P.CAR_TELEMETRY.pack(speed, .9, 0.0, 0.0, 0, 7, 11000, 0, 50, 0, 0, 0, 0, 0, 90, 90, 90, 90, 100, 100, 100, 100, 100, 23, 23, 23, 23, 0, 0, 0, 0)
    return header(6, player) + b"".join(one if i == player else bytes(60) for i in range(22)) + bytes(3)


def stint_laps(mean: float) -> list[tuple[float, tuple[float, float, float], int]]:
    """An in-lap-0 lap, an out-lap, then three valid flying laps around ``mean`` seconds."""
    def lap(t, flags=15):
        return (t, (t * 0.31, t * 0.39, t * 0.30), flags)
    return [lap(mean + 4), lap(mean + 2), lap(mean + 0.1), lap(mean - 0.1), lap(mean)]
