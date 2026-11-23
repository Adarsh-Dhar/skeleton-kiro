"""Synthetic vehicle-performance model (invented; not real aerodynamics or tyre physics).

Design intent: lap time has an interior optimum (softer suspension, ~21 psi, moderate wing and balance) away from the baseline setup, so a screening design finds the important
factors and a model-based optimiser can refine; stability *rises* with downforce, so a minimum-stability constraint
forces a genuine lap-time trade-off.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any

FACTORS: dict[str, dict[str, Any]] = {
    "front_wing": {"low": 0.0, "high": 50.0, "unit": "deg", "label": "Front wing angle", "default": 25.0},
    "rear_wing": {"low": 0.0, "high": 50.0, "unit": "deg", "label": "Rear wing angle", "default": 29.0},
    "suspension": {"low": 1.0, "high": 11.0, "unit": "stiffness", "label": "Suspension stiffness", "default": 6.0},
    "tyre_pressure_psi": {"low": 18.0, "high": 26.0, "unit": "psi", "label": "Tyre pressure", "default": 22.0},
}
BASELINE = {k: v["default"] for k, v in FACTORS.items()}
METRIC_UNITS = {
    "lap_time": "seconds", "sector_1": "seconds", "sector_2": "seconds", "sector_3": "seconds",
    "stability": "score", "top_speed": "km/h", "tyre_degradation": "percent",
}  # fmt: skip


def rng_for(*parts: Any) -> random.Random:
    """Deterministic RNG from arbitrary parts (same run id + setup => same synthetic data)."""
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16], 16))


def vehicle_model(setup: dict[str, float]) -> dict[str, float]:
    """Noise-free expected performance for a setup (missing factors use the baseline setup)."""
    s = {**BASELINE, **setup}
    fw, rw, sus, tp = s["front_wing"], s["rear_wing"], s["suspension"], s["tyre_pressure_psi"]
    wing = fw + rw
    bal = (rw - fw) - 4.0  # ideal: rear 4 deg more than front
    s1 = 26.0 + 0.020 * wing + 0.004 * (sus - 4.5) ** 2
    s2 = 34.0 - 0.120 * wing + 0.0015 * wing**2 + 0.004 * bal**2 + 0.060 * (sus - 4.5) ** 2 + 0.050 * (tp - 21) ** 2
    s3 = 21.0 + 0.012 * wing + 0.020 * (tp - 21) ** 2 + 0.020 * (sus - 4.5) ** 2
    stability = 70.0 + 0.35 * wing - 0.03 * bal**2 - 0.5 * (sus - 6) ** 2 - 0.8 * abs(tp - 22)
    return {
        "sector_1": s1, "sector_2": s2, "sector_3": s3, "lap_time": s1 + s2 + s3,
        "stability": max(0.0, min(100.0, stability)),
        "top_speed": 340.0 - 0.45 * wing - 0.15 * (sus - 6) ** 2,
        "tyre_degradation": 8.0 + 0.12 * (sus - 6) ** 2 + 0.15 * (tp - 22) ** 2 + 0.03 * wing,
    }  # fmt: skip


def stint(setup: dict[str, float], run_id: str, laps: int, outlier_prob: float) -> list[dict[str, float]]:
    """Per-lap sector/lap times for a stint (tyre wear adds a small drift; rare slow laps model traffic/errors)."""
    truth = vehicle_model(setup)
    out = []
    for lap in range(1, laps + 1):
        r = rng_for(run_id, "lap", lap)
        drift = 0.012 * lap * truth["tyre_degradation"] / 10.0
        sec = {k: truth[k] + r.gauss(0, 0.05) for k in ("sector_1", "sector_2", "sector_3")}
        sec["sector_3"] += drift
        if r.random() < outlier_prob:
            sec["sector_2"] += 1.5 + 2 * r.random()  # a slow lap
        out.append({"lap": lap, **sec, "lap_time": sum(sec.values())})
    return out


def run_level(setup: dict[str, float], run_id: str) -> dict[str, float]:
    """Once-per-stint measurements (stability, top speed, tyre degradation) with measurement noise."""
    truth = vehicle_model(setup)
    r = rng_for(run_id, "stint")
    return {
        "stability": max(0.0, min(100.0, truth["stability"] + r.gauss(0, 0.4))),
        "top_speed": truth["top_speed"] + r.gauss(0, 0.5),
        "tyre_degradation": max(0.0, truth["tyre_degradation"] + r.gauss(0, 0.15)),
    }
