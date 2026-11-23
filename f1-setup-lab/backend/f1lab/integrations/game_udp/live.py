"""Live state: turns decoded packets into 'current setup', 'laps so far', telemetry samples and a stint recorder."""
from __future__ import annotations

from f1lab.integrations.game_udp import protocols as P
from f1lab.integrations.game_udp.capture import StintRecorder, StintResult
from f1lab.integrations.game_udp.telemetry import TelemetryBuffer


class LiveState:
    def __init__(self) -> None:
        self.setup: dict | None = None
        self.baseline: dict | None = None  # first setup seen = the baseline every experiment run is compared with
        self.history: P.History | None = None
        self.top_speed_kph = 0.0
        self.recorder: StintRecorder | None = None
        self.session_uid: int | None = None
        self.player_car: int | None = None
        self.packet_format: int | None = None
        self.telemetry = TelemetryBuffer()
        self.stint_telemetry: TelemetryBuffer | None = None
        self.last_stint_telemetry: TelemetryBuffer | None = None

    def _sync_session(self, h: P.Header) -> None:
        """Reset per-session state when the game starts a new session/car/format so laps and baselines never mix."""
        if self.session_uid is None:
            self.session_uid, self.player_car, self.packet_format = h.session_uid, h.player_car, h.packet_format
            return
        if (h.session_uid, h.player_car, h.packet_format) == (self.session_uid, self.player_car, self.packet_format):
            return
        self.setup = self.baseline = self.history = None
        self.top_speed_kph = 0.0
        self.telemetry.clear()
        self.stint_telemetry = None
        if self.recorder is not None:
            self.recorder.session_changed = True
        self.session_uid, self.player_car, self.packet_format = h.session_uid, h.player_car, h.packet_format

    def on_packet(self, h: P.Header, data: bytes) -> None:
        self._sync_session(h)
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
        elif h.packet_id == P.PID_TELEMETRY:
            decoded = P.decode_telemetry(data, h)
            self.telemetry.add(h, decoded)
            if self.recorder and self.recorder.start_lap is not None and self.recorder.end_lap is None:
                if self.stint_telemetry is None:
                    self.stint_telemetry = TelemetryBuffer()
                self.stint_telemetry.add(h, decoded)
                self.top_speed_kph = max(self.top_speed_kph, float(decoded["speed_kph"]))

    def start_stint(self, target: dict) -> None:
        if self.baseline is None or self.history is None:
            raise RuntimeError("no setup/lap data received yet: is the game sending UDP telemetry?")
        self.top_speed_kph = 0.0
        self.stint_telemetry = TelemetryBuffer()
        self.recorder = StintRecorder(target, self.baseline)
        self.recorder.on_history(self.history)
        self.recorder.start()

    def stop_stint(self) -> tuple[StintResult, float]:
        if not self.recorder:
            raise RuntimeError("no stint in progress")
        result, self.recorder = self.recorder.stop(), None
        self.last_stint_telemetry = self.stint_telemetry
        self.stint_telemetry = None
        return result, self.top_speed_kph
