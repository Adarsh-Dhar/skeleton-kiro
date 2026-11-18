"""Bounded telemetry buffer for decoded game packets. Raw packets remain the source of truth; this is for live analysis."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from f1lab.integrations.game_udp.protocols import Header


@dataclass(frozen=True)
class TelemetrySample:
    session_time: float
    frame_id: int
    packet_format: int
    packet_id: int
    session_uid: int
    speed_kph: float
    throttle: float
    steer: float
    brake: float
    gear: int
    rpm: float
    clutch: int | None = None
    drs: int | None = None
    rev_lights_percent: int | None = None
    engine_temperature_c: float | None = None
    brake_temperature_c: tuple[int, ...] = ()
    tyre_surface_temperature_c: tuple[int, ...] = ()
    tyre_inner_temperature_c: tuple[int, ...] = ()
    tyre_pressure_psi: tuple[float, ...] = ()
    surface_type: tuple[int, ...] = ()


class TelemetryBuffer:
    def __init__(self, max_samples: int = 20_000) -> None:
        self.samples: deque[TelemetrySample] = deque(maxlen=max_samples)

    def add(self, h: Header, decoded: dict[str, object]) -> None:
        self.samples.append(
            TelemetrySample(
                session_time=h.session_time,
                frame_id=h.frame_id,
                packet_format=h.packet_format,
                packet_id=h.packet_id,
                session_uid=h.session_uid,
                speed_kph=float(decoded.get("speed_kph", 0.0)),
                throttle=float(decoded.get("throttle", 0.0)),
                steer=float(decoded.get("steer", 0.0)),
                brake=float(decoded.get("brake", 0.0)),
                gear=int(decoded.get("gear", 0)),
                rpm=float(decoded.get("rpm", 0.0)),
                clutch=decoded.get("clutch"),
                drs=decoded.get("drs"),
                rev_lights_percent=decoded.get("rev_lights_percent"),
                engine_temperature_c=decoded.get("engine_temperature_c"),
                brake_temperature_c=tuple(decoded.get("brake_temperature_c", ())),
                tyre_surface_temperature_c=tuple(decoded.get("tyre_surface_temperature_c", ())),
                tyre_inner_temperature_c=tuple(decoded.get("tyre_inner_temperature_c", ())),
                tyre_pressure_psi=tuple(decoded.get("tyre_pressure_psi", ())),
                surface_type=tuple(decoded.get("surface_type", ())),
            )
        )

    def clear(self) -> None:
        self.samples.clear()

    def summary(self) -> dict[str, object]:
        n = len(self.samples)
        if not n:
            return {"samples": 0}
        speeds = [s.speed_kph for s in self.samples]
        throttles = [s.throttle for s in self.samples]
        brakes = [s.brake for s in self.samples]
        rpms = [s.rpm for s in self.samples]
        return {
            "samples": n,
            "packet_formats": sorted({s.packet_format for s in self.samples}),
            "first_session_time": round(self.samples[0].session_time, 3),
            "last_session_time": round(self.samples[-1].session_time, 3),
            "duration_s": round(max(0.0, self.samples[-1].session_time - self.samples[0].session_time), 3),
            "top_speed_kph": round(max(speeds), 1),
            "mean_throttle": round(sum(throttles) / n, 4),
            "mean_brake": round(sum(brakes) / n, 4),
            "max_rpm": round(max(rpms), 1),
        }
