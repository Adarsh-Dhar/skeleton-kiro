"""SYNTHETIC DEMO DATA GENERATOR - NOT REAL DATA.

Writes ``02_demo_meter_readings_30min.csv``: a fabricated household for 1-31 Oct 2026 (Asia/Kolkata, 30-minute interval
energy) that matches the demo pack: 1488 rows, a constant 0.40 kW load from 00:00 to 04:59, a 1.6 kWh dishwasher cycle at
20:00-22:00 every day, and 446.71 kWh in total. It exists only to exercise WATTWISE in a sandbox database. Never import it into
a real installation or describe it as a meter reading.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
IST = timezone(timedelta(hours=5, minutes=30))
TOTAL_KWH = 446.71
NIGHT_KWH = 0.2  # 0.40 kW for 30 minutes
DISH_KWH = 0.4  # 1.6 kWh over four 30-minute slots (0.8 kW average)


def shape(hour: float) -> float:
    """Relative daytime load: morning and evening peaks (arbitrary synthetic shape)."""
    return 0.6 + 0.9 * math.exp(-((hour - 7.5) ** 2) / 2.0) + 1.4 * math.exp(-((hour - 19.5) ** 2) / 3.0)


def build() -> list[tuple[datetime, float]]:
    rng = random.Random(20261001)
    start = datetime(2026, 10, 1, tzinfo=IST)
    slots = [start + timedelta(minutes=30 * i) for i in range(31 * 48)]
    fixed: dict[int, float] = {}
    free: dict[int, float] = {}
    for i, t in enumerate(slots):
        h = t.hour + t.minute / 60
        if h < 5:
            fixed[i] = NIGHT_KWH
        else:
            free[i] = shape(h) * (0.85 + 0.3 * rng.random()) * (0.9 + 0.2 * math.sin(t.day))
            if 20 <= h < 22:
                fixed[i] = DISH_KWH
    remaining = TOTAL_KWH - sum(fixed.values())
    scale = remaining / sum(free.values())
    values = [round(fixed.get(i, 0.0) + free.get(i, 0.0) * scale, 4) for i in range(len(slots))]
    values[-1] = round(values[-1] + TOTAL_KWH - sum(values), 4)  # exact total after rounding
    return list(zip(slots, values, strict=True))


def main() -> None:
    rows = build()
    lines = ["timestamp,energy_kwh"] + [f"{t.strftime('%Y-%m-%dT%H:%M')}+05:30,{v:.4f}" for t, v in rows]
    (HERE / "02_demo_meter_readings_30min.csv").write_text("\n".join(lines) + "\n")
    print(f"{len(rows)} rows, total {sum(v for _, v in rows):.2f} kWh")


if __name__ == "__main__":
    main()
