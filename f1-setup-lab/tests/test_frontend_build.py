"""An open browser tab must never keep running old JavaScript after the frontend files change."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from f1lab.api.app import create_app

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


def test_frontend_fingerprint_changes_when_a_file_changes(tmp_path):
    fe = tmp_path / "frontend"
    shutil.copytree(FRONTEND, fe)
    with TestClient(create_app(str(tmp_path / "t.db"), frontend_dir=str(fe))) as c:
        first = c.get("/api/frontend-build").json()
        assert first["files"] >= 5 and len(first["build"]) == 16
        assert c.get("/api/frontend-build").json() == first  # stable while nothing changes
        js = fe / "app.js"
        js.write_text(js.read_text() + "\n// updated\n")
        os.utime(js, ns=(js.stat().st_atime_ns, js.stat().st_mtime_ns + 1_000_000_000))
        assert c.get("/api/frontend-build").json()["build"] != first["build"]
        assert c.get("/app.js").headers["cache-control"] == "no-cache"
