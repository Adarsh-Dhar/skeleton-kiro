"""Raw packet recording: every datagram is stored with its receive time so any session can be re-decoded later (e.g. after a decoder fix)."""
from __future__ import annotations

import struct
import time
from collections.abc import Iterator
from pathlib import Path

REC = struct.Struct("<dI")  # receive time (unix seconds), datagram length


class RawRecorder:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self._f = path, path.open("ab")

    def write(self, data: bytes, received: float | None = None) -> None:
        self._f.write(REC.pack(received or time.time(), len(data)) + data)

    def close(self) -> None:
        self._f.close()


def read_raw(path: Path) -> Iterator[tuple[float, bytes]]:
    with path.open("rb") as f:
        while head := f.read(REC.size):
            if len(head) < REC.size:
                return  # truncated tail from a crash: ignore
            t, n = REC.unpack(head)
            data = f.read(n)
            if len(data) < n:
                return
            yield t, data
