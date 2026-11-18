"""Verify the game's setup fields before you trust them.

    python scripts/probe_setup.py [--host 127.0.0.1] [--port 20777]

Start a session in EA SPORTS F1 25 with UDP telemetry on (F1 25 format), go to the garage and change ONE control at a time to its lowest and highest
position. This prints every change the game reports. For each factor confirm: the value moves, it equals the number shown in the game, and no other
field moves with it. Only then set ``verified`` to True for that factor in ``backend/f1lab/catalogs/ea_f1_25.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from f1lab.integrations.game_udp.live import LiveState  # noqa: E402
from f1lab.integrations.game_udp.receiver import UdpReceiver  # noqa: E402


async def main(host: str, port: int, seconds: float | None) -> None:
    live, last = LiveState(), {}

    def on_packet(h, data):
        nonlocal last
        live.on_packet(h, data)
        if live.setup and live.setup != last:
            changed = {k: (last.get(k), v) for k, v in live.setup.items() if last.get(k) != v}
            if last:
                print("changed: " + ", ".join(f"{k} {a} -> {b}" for k, (a, b) in changed.items()), flush=True)
            else:
                print("first setup received:\n" + "\n".join(f"  {k:24s}{v}" for k, v in live.setup.items()), flush=True)
            last = dict(live.setup)

    rx = UdpReceiver(on_packet)
    await rx.start(host, port)
    print(f"listening on {host}:{port}; change one control at a time in the garage (Ctrl+C to stop)", flush=True)
    try:
        await asyncio.sleep(seconds) if seconds else await asyncio.Event().wait()
    finally:
        st = rx.status()
        rx.stop()
        print(f"\npackets accepted {st['packets']}, wrong format {st['wrong_format']}, malformed {st['malformed']}")
        if st["wrong_format"]:
            print("The game is sending a different UDP format: select the F1 25 format (not the 2026 Season Pack).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=20777)
    ap.add_argument("--seconds", type=float, default=None, help="stop after this many seconds (for tests)")
    a = ap.parse_args()
    try:
        asyncio.run(main(a.host, a.port, a.seconds))
    except KeyboardInterrupt:
        pass
