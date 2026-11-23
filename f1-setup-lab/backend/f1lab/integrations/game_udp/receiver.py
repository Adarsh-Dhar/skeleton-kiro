"""Asyncio UDP receiver for the game's telemetry output. Never raises on bad input; counts and reports instead."""
from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Callable

from f1lab.integrations.game_udp import protocols as P
from f1lab.integrations.game_udp.recorder import RawRecorder

ACCEPTED_FORMATS = P.accepted_formats()  # packet-format numbers with a registered decoder


class UdpReceiver(asyncio.DatagramProtocol):
    def __init__(self, on_packet: Callable[[P.Header, bytes], None], recorder: RawRecorder | None = None) -> None:
        self.on_packet, self.recorder = on_packet, recorder
        self.stats = {"packets": 0, "malformed": 0, "wrong_format": 0, "last_packet_at": None, "last_error": None}
        self.by_id: Counter[int] = Counter()
        self.transport: asyncio.DatagramTransport | None = None

    # -- asyncio callbacks
    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.stats["last_packet_at"] = time.time()
        try:
            h = P.parse_header(data)
        except P.PacketError as exc:
            self.stats["malformed"] += 1
            self.stats["last_error"] = str(exc)
            return
        if h.packet_format not in ACCEPTED_FORMATS:
            self.stats["wrong_format"] += 1
            self.stats["last_error"] = f"packet format {h.packet_format} is not supported (select the matching UDP format in the game)"
            return
        self.stats["packets"] += 1
        self.by_id[h.packet_id] += 1
        if self.recorder:
            self.recorder.write(data, self.stats["last_packet_at"])
        try:
            self.on_packet(h, data)
        except P.PacketError as exc:  # a decoder rejected the packet (wrong size etc.): count it, keep listening
            self.stats["malformed"] += 1
            self.stats["last_error"] = str(exc)

    def error_received(self, exc) -> None:
        self.stats["last_error"] = str(exc)

    # -- lifecycle
    async def start(self, host: str = "127.0.0.1", port: int = 20777) -> int:
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: self, local_addr=(host, port))
        return self.transport.get_extra_info("sockname")[1]

    def stop(self) -> None:
        if self.transport:
            self.transport.close()
        if self.recorder:
            self.recorder.close()

    def status(self) -> dict:
        age = None if self.stats["last_packet_at"] is None else round(time.time() - self.stats["last_packet_at"], 1)
        return {**self.stats, "seconds_since_last_packet": age, "connected": age is not None and age < 3, "by_packet_id": dict(self.by_id)}
