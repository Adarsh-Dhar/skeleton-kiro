"""Raw packet recording into the root database (``f1_udp_recording`` / ``f1_udp_packet``).

Every accepted datagram is stored with its receive time so any session can be re-decoded later (e.g. after a decoder
fix). Writes go through a background thread in batches, so the asyncio UDP callback never waits on the database.

``read_raw_file`` reads the legacy ``.f1raw`` file format (``<dI`` receive time + length, then the datagram); it is
kept only so ``scripts/migrate_to_root_db.py`` and ``scripts/inspect_udp_recording.py`` can import old captures.
"""
from __future__ import annotations

import queue
import struct
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, func, select

from db.engine import immediate_transaction
from db.models.f1 import UdpPacket, UdpRecording

REC = struct.Struct("<dI")  # legacy .f1raw: receive time (unix seconds), datagram length
BATCH = 500
FLUSH_SECONDS = 0.5


def new_recording_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


class RawRecorder:
    """Append-only recorder for one capture session. ``write`` is non-blocking; ``close`` flushes everything."""

    def __init__(self, engine: Engine, recording_id: str | None = None, *, host: str | None = None, port: int | None = None,
                 source: str = "live", note: str | None = None) -> None:
        self.engine = engine
        self.recording_id = recording_id or new_recording_id()
        with immediate_transaction(engine) as c:
            exists = c.execute(select(UdpRecording.recording_id).where(UdpRecording.recording_id == self.recording_id)).scalar()
            if exists is None:
                c.execute(UdpRecording.__table__.insert().values(recording_id=self.recording_id, started_at=datetime.now(UTC),
                                                                 host=host, port=port, source=source, note=note))
            self._seq = int(c.execute(select(func.coalesce(func.max(UdpPacket.seq), -1)).where(UdpPacket.recording_id == self.recording_id)).scalar()) + 1
        self._q: queue.Queue = queue.Queue()
        self._closed = False
        self.errors = 0
        self.written = 0
        self._thread = threading.Thread(target=self._run, name=f"udp-recorder-{self.recording_id}", daemon=True)
        self._thread.start()

    def write(self, data: bytes, received: float | None = None) -> None:
        if self._closed:
            return
        self._q.put((self._seq, received or time.time(), bytes(data)))
        self._seq += 1

    def _flush(self, batch: list) -> None:
        if not batch:
            return
        try:
            with immediate_transaction(self.engine) as c:
                c.exec_driver_sql("INSERT INTO f1_udp_packet (recording_id, seq, received_at, data) VALUES (?, ?, ?, ?)",
                                  [(self.recording_id, s, t, d) for s, t, d in batch])
            self.written += len(batch)
        except Exception:  # never take the receiver down; the count is reported in status()
            self.errors += len(batch)

    def _run(self) -> None:
        batch: list = []
        last = time.monotonic()
        while True:
            try:
                item = self._q.get(timeout=FLUSH_SECONDS)
            except queue.Empty:
                item = None
            if item is _STOP:
                self._flush(batch)
                return
            if item is not None:
                batch.append(item)
            if len(batch) >= BATCH or (batch and time.monotonic() - last >= FLUSH_SECONDS):
                self._flush(batch)
                batch, last = [], time.monotonic()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._q.put(_STOP)
        self._thread.join(timeout=30)


_STOP = object()


def read_raw(engine: Engine, recording_id: str) -> Iterator[tuple[float, bytes]]:
    """(receive time, datagram) in recording order."""
    with engine.connect() as c:
        for t, d in c.execute(select(UdpPacket.received_at, UdpPacket.data).where(UdpPacket.recording_id == recording_id).order_by(UdpPacket.seq)):
            yield t, bytes(d)


def list_recordings(engine: Engine) -> list[dict]:
    q = (select(UdpRecording.recording_id, UdpRecording.started_at, UdpRecording.source, UdpRecording.host, UdpRecording.port,
                UdpRecording.note, func.count(UdpPacket.seq))
         .select_from(UdpRecording).outerjoin(UdpPacket, UdpPacket.recording_id == UdpRecording.recording_id)
         .group_by(UdpRecording.recording_id).order_by(UdpRecording.started_at.desc()))
    with engine.connect() as c:
        return [{"recording_id": r, "started_at": str(s), "source": src, "host": h, "port": p, "note": n, "packets": cnt}
                for r, s, src, h, p, n, cnt in c.execute(q)]


def read_raw_file(path: Path) -> Iterator[tuple[float, bytes]]:
    """Legacy ``.f1raw`` reader (import/inspection only). A truncated tail from a crash is ignored."""
    with Path(path).open("rb") as f:
        while head := f.read(REC.size):
            if len(head) < REC.size:
                return
            t, n = REC.unpack(head)
            data = f.read(n)
            if len(data) < n:
                return
            yield t, data


def write_raw_file(path: Path, packets: list[tuple[float, bytes]]) -> Path:
    """Write the legacy format (used only to export a recording or build test fixtures)."""
    path = Path(path)
    with path.open("wb") as f:
        for t, d in packets:
            f.write(REC.pack(t, len(d)) + d)
    return path


def import_raw_file(engine: Engine, path: Path, recording_id: str | None = None, note: str | None = None) -> tuple[str, int]:
    """Copy a legacy ``.f1raw`` capture into the database. Returns (recording_id, packets)."""
    rid = recording_id or Path(path).parent.name or new_recording_id()
    r = RawRecorder(engine, rid, source="import", note=note or f"imported from {Path(path).name}")
    n = 0
    for t, d in read_raw_file(path):
        r.write(d, t)
        n += 1
    r.close()
    return rid, n
