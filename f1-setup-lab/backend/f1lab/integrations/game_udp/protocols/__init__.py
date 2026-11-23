"""Versioned packet decoders, one module per UDP format."""
from __future__ import annotations

from f1lab.integrations.game_udp.protocols import f1_25, f1_25_2026

DECODERS = {2025: f1_25, 2026: f1_25_2026}

Header = f1_25.Header
History = f1_25.History
LapRecord = f1_25.LapRecord
PacketError = f1_25.PacketError

PID_SESSION = f1_25.PID_SESSION
PID_LAP_DATA = f1_25.PID_LAP_DATA
PID_SETUPS = f1_25.PID_SETUPS
PID_TELEMETRY = f1_25.PID_TELEMETRY
PID_HISTORY = f1_25.PID_HISTORY

__all__ = [
    "DECODERS",
    "Header",
    "History",
    "LapRecord",
    "PacketError",
    "PID_HISTORY",
    "PID_LAP_DATA",
    "PID_SESSION",
    "PID_SETUPS",
    "PID_TELEMETRY",
    "accepted_formats",
    "decode_history",
    "decode_setup",
    "decode_telemetry",
    "decoder_for",
    "parse_header",
]


def accepted_formats() -> set[int]:
    return set(DECODERS)


def decoder_for(packet_format: int):
    try:
        return DECODERS[packet_format]
    except KeyError as exc:
        raise PacketError(f"unsupported packet format {packet_format}") from exc


def parse_header(data: bytes) -> Header:
    # The 29-byte header is identical across the supported F1 25 formats.
    return f1_25.parse_header(data)


def decode_setup(data: bytes, h: Header) -> dict[str, float]:
    return decoder_for(h.packet_format).decode_setup(data, h)


def decode_telemetry(data: bytes, h: Header) -> dict[str, object]:
    return decoder_for(h.packet_format).decode_telemetry(data, h)


def decode_history(data: bytes, h: Header) -> History | None:
    return decoder_for(h.packet_format).decode_history(data, h)
