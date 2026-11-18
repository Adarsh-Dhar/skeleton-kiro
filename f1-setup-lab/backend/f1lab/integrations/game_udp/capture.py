"""Setup verification + stint capture. No SDK imports: pure logic, testable without the game."""
from __future__ import annotations

from dataclasses import dataclass, field

from f1lab.integrations.game_udp.protocols.f1_25 import History, LapRecord

TOL = {"front_camber": 0.011, "rear_camber": 0.011, "front_toe": 0.011, "rear_toe": 0.011, "fuel_load": 0.5,
       "rear_left_tyre_psi": 0.06, "rear_right_tyre_psi": 0.06, "front_left_tyre_psi": 0.06, "front_right_tyre_psi": 0.06}


def same(a: dict, b: dict, fields=None) -> list[str]:
    """Names of fields that differ (integers must match exactly, floats within TOL)."""
    return [k for k in (fields or a) if abs(float(a[k]) - float(b[k])) > TOL.get(k, 0.0)]


@dataclass
class StintResult:
    laps: list[LapRecord]; accepted: bool; reasons: list[str]; setups_seen: int; start_lap: int; end_lap: int


@dataclass
class StintRecorder:
    """Records one stint. ``target`` is only the factors under test; every other setup field must equal ``baseline``."""
    target: dict; baseline: dict; min_valid_laps: int = 3; skip_out_lap: bool = True
    snapshots: list[dict] = field(default_factory=list); history: History | None = None
    start_lap: int | None = None; end_lap: int | None = None

    def on_setup(self, setup: dict) -> None:
        if self.start_lap is not None and self.end_lap is None and (not self.snapshots or same(self.snapshots[-1], setup)):
            self.snapshots.append(dict(setup))

    def on_history(self, h: History) -> None:
        self.history = h

    def start(self) -> None:
        self.start_lap, self.end_lap, self.snapshots = len(self.history.laps) if self.history else 0, None, []

    def stop(self) -> StintResult:
        self.end_lap = len(self.history.laps) if self.history else 0
        why: list[str] = []
        if not self.snapshots:
            why.append("no setup packet was received during the stint (check UDP settings)")
        else:
            for a, b in zip(self.snapshots, self.snapshots[1:]):
                if same(a, b):
                    why.append(f"setup changed during the stint: {', '.join(same(a, b))}")
                    break
            snap = self.snapshots[-1]
            wrong = same({**snap}, {**self.baseline, **self.target}, list(self.target))
            if wrong:
                why.append("setup does not match the target for " + ", ".join(f"{k} (game {snap[k]} vs target {self.target[k]})" for k in wrong))
            drift = same(snap, self.baseline, [k for k in snap if k not in self.target])
            if drift:
                why.append("fields outside the experiment differ from the baseline: " + ", ".join(drift))
        laps = [lap for lap in (self.history.laps if self.history else []) if self.start_lap < lap.lap <= self.end_lap]
        if self.skip_out_lap and laps:
            laps = laps[1:]
        valid = [lap for lap in laps if lap.valid]
        if len(valid) < self.min_valid_laps:
            why.append(f"only {len(valid)} valid timed laps (need {self.min_valid_laps})")
        return StintResult(valid, not why, why, len(self.snapshots), self.start_lap, self.end_lap)
