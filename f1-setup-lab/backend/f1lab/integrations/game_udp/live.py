"""Live state: turns decoded packets into 'current setup', 'laps so far' and a stint recorder."""
from __future__ import annotations

from f1lab.integrations.game_udp.capture import StintRecorder, StintResult
from f1lab.integrations.game_udp.protocols import f1_25 as P


class LiveState:
    def __init__(self) -> None:
        self.setup: dict | None = None
        self.baseline: dict | None = None  # first setup seen = the baseline every experiment run is compared with
        self.history: P.History | None = None
        self.top_speed_kph = 0.0
        self.recorder: StintRecorder | None = None

    def on_packet(self, h: P.Header, data: bytes) -> None:
        if h.packet_id == P.PID_SETUPS:
            self.setup = P.decode_setup(data, h)
            self.baseline = self.baseline or dict(self.setup)
            if self.recorder:
                self.recorder.on_setup(self.setup)
        elif h.packet_id == P.PID_HISTORY:
            hist = P.decode_history(data, h)
            if hist is not None:
                self.history = hist
                if self.recorder:
                    self.recorder.on_history(hist)
        elif h.packet_id == P.PID_TELEMETRY and self.recorder and self.recorder.start_lap is not None and self.recorder.end_lap is None:
            self.top_speed_kph = max(self.top_speed_kph, P.decode_telemetry(data, h)["speed_kph"])

    def start_stint(self, target: dict) -> None:
        if self.baseline is None or self.history is None:
            raise RuntimeError("no setup/lap data received yet: is the game sending UDP telemetry?")
        self.top_speed_kph = 0.0
        self.recorder = StintRecorder(target, self.baseline)
        self.recorder.on_history(self.history)
        self.recorder.start()

    def stop_stint(self) -> tuple[StintResult, float]:
        if not self.recorder:
            raise RuntimeError("no stint in progress")
        result, self.recorder = self.recorder.stop(), None
        return result, self.top_speed_kph
