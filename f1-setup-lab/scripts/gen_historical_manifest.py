"""Generate tests/fixtures/historical_routes.json: the contract of every /api/historical/* route of the app as it is now.

    python scripts/gen_historical_manifest.py [--out tests/fixtures/historical_routes.json] [--check]

For each route it records the method, path, request schema (query/path parameters and JSON body) and response schema, taken from the
app's OpenAPI document. For each GET route and each OpenF1 fixture session (tests/fixtures/openf1/*.json) it then imports the fixture
through a fake OpenF1 server (no network), calls the route and records the top-level response keys and their value types.
Only types are recorded, never values, so the file stays stable. ``--check`` compares instead of writing (exit 1 on a difference).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT.parent))

from fastapi.testclient import TestClient  # noqa: E402

from f1lab.api.app import create_app  # noqa: E402
from f1lab.integrations.historical.openf1_client import OpenF1Client, RateLimiter  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "openf1"
DEFAULT_OUT = ROOT / "tests" / "fixtures" / "historical_routes.json"
DATASET_NAMES = ["race_position", "session_result", "starting_grid", "intervals", "overtakes", "team_radio",
                 "drivers_championship", "teams_championship", "pit_stops", "race_control"]
CODES = {"Race": "R", "Qualifying": "Q", "Sprint": "S"}
_COND = re.compile(r"^(\w+)(>=|<=|>|<|=)(.*)$")


# ----------------------------------------------------------------------------------------------- fake OpenF1
def fixture_fetch(d: dict):
    """``fetch(url, timeout)`` answering from one fixture file, like api.openf1.org would for that session."""
    def fetch(url: str, timeout: float):
        path, _, query = url.partition("?")
        endpoint = path.rsplit("/", 1)[-1]
        conds = [m.groups() for part in query.split("&") if part and (m := _COND.match(unquote(part)))]
        eq = {k: v for k, op, v in conds if op == "="}
        if endpoint == "sessions":
            rows = list(d["meeting_sessions"] if "meeting_key" in eq else d["sessions"])
        else:
            rows = list(d.get(endpoint, []))
        if "driver_number" in eq:
            rows = [r for r in rows if str(r.get("driver_number")) == eq["driver_number"]]
        for k, op, v in conds:
            if k == "date" and op in (">=", "<"):
                cut = datetime.fromisoformat(v).replace(tzinfo=UTC)
                rows = [r for r in rows if (datetime.fromisoformat(r["date"]) >= cut) == (op == ">=")]
        if not rows:
            return 404, {}, json.dumps({"detail": "No results found."}).encode()
        return 200, {}, json.dumps(rows).encode()
    return fetch


# ----------------------------------------------------------------------------------------------- schemas
def resolve(node, comps: dict, seen: tuple = ()):
    """Inline $ref pointers of an OpenAPI schema (recursive references are left as the component name)."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if ref:
            name = ref.rsplit("/", 1)[-1]
            return {"$ref": name} if name in seen else resolve(comps[name], comps, seen + (name,))
        return {k: resolve(v, comps, seen) for k, v in node.items() if k not in ("title", "examples")}
    if isinstance(node, list):
        return [resolve(v, comps, seen) for v in node]
    return node


def route_contract(openapi: dict) -> list[dict]:
    comps = openapi.get("components", {}).get("schemas", {})
    out = []
    for path, item in openapi["paths"].items():
        if not path.startswith("/api/historical/"):
            continue
        for method, op in item.items():
            params = [{"name": p["name"], "in": p["in"], "required": p.get("required", False), "schema": resolve(p.get("schema", {}), comps)}
                      for p in op.get("parameters", [])]
            body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
            responses = {}
            for code, r in op.get("responses", {}).items():
                schema = r.get("content", {}).get("application/json", {}).get("schema")
                responses[code] = resolve(schema, comps) if schema else None
            out.append({"method": method.upper(), "path": path,
                        "request": {"parameters": sorted(params, key=lambda p: (p["in"], p["name"])), "body": resolve(body, comps) if body else None},
                        "response": responses})
    return sorted(out, key=lambda r: (r["path"], r["method"]))


def type_name(v) -> str:
    if v is None:
        return "null"
    return {bool: "bool", int: "int", float: "float", str: "str", list: "list", dict: "dict"}.get(type(v), type(v).__name__)


def shape(body) -> dict:
    """Top-level keys with value types. A list response records the union of its items' keys and types."""
    if isinstance(body, dict):
        return {"kind": "object", "keys": {k: type_name(v) for k, v in sorted(body.items())}}
    if isinstance(body, list):
        keys: dict[str, set] = {}
        for item in body:
            if isinstance(item, dict):
                for k, v in item.items():
                    keys.setdefault(k, set()).add(type_name(v))
        return {"kind": "array", "item_keys": {k: sorted(t) for k, t in sorted(keys.items())}, "item_types": sorted({type_name(i) for i in body})}
    return {"kind": type_name(body)}


# ----------------------------------------------------------------------------------------------- observed responses
def observe(label: str, d: dict, tmp: Path) -> dict:
    sess, meeting = d["sessions"][0], d["meetings"][0]
    year, event, code = int(sess["year"]), meeting["meeting_name"], CODES[sess["session_name"]]
    num = str(d["_meta"]["driver_number"])
    driver = next(x["name_acronym"] for x in d["drivers"] if str(x["driver_number"]) == num)
    client = OpenF1Client(tmp / "cache", fetch=fixture_fetch(d), limiter=RateLimiter(per_second=10_000, per_minute=10_000_000), sleep=lambda s: None)
    app = create_app(str(tmp / "trialforge.db"), frontend_dir=str(tmp / "no-frontend"), openf1_client_factory=lambda: client)
    seen: dict[str, dict] = {}

    with TestClient(app) as http:
        def get(key: str, url: str, **kw) -> None:
            r = http.get(url, **kw)
            seen[key] = {"status": r.status_code, **shape(r.json())}

        pre = "/api/historical"
        get(f"GET {pre}/events", f"{pre}/events", params={"year": year})
        get(f"GET {pre}/event-sessions", f"{pre}/event-sessions", params={"year": year, "event": event})
        get(f"GET {pre}/drivers", f"{pre}/drivers", params={"year": year, "event": event, "session": code})
        iid = http.post(f"{pre}/import", json={"year": year, "event": event, "session": code, "drivers": [driver]}).json()["import_id"]
        st: dict = {}
        for _ in range(600):
            st = http.get(f"{pre}/imports/{iid}").json()
            if st["state"] in ("COMPLETED", "FAILED", "CANCELLED"):
                break
            time.sleep(0.1)
        if st.get("state") != "COMPLETED":
            raise SystemExit(f"{label}: import ended in state {st.get('state')}: {st}")
        sid = st["session_id"]
        base = f"{pre}/sessions/{sid}"
        gen = f"{pre}/sessions/{{session_id}}"
        get(f"GET {pre}/openf1/cache", f"{pre}/openf1/cache")
        get(f"GET {pre}/imports", f"{pre}/imports")
        get(f"GET {pre}/imports/{{import_id}}", f"{pre}/imports/{iid}")
        get(f"GET {pre}/sessions", f"{pre}/sessions")
        get(f"GET {gen}", base)
        get(f"GET {gen}/drivers", f"{base}/drivers")
        get(f"GET {gen}/laps", f"{base}/laps")
        get(f"GET {gen}/telemetry", f"{base}/telemetry", params={"driver": driver})
        get(f"GET {gen}/position", f"{base}/position", params={"driver": driver})
        get(f"GET {gen}/weather", f"{base}/weather")
        get(f"GET {gen}/track-map", f"{base}/track-map")
        by_name = {}
        for name in DATASET_NAMES:
            r = http.get(f"{base}/datasets/{name}")
            by_name[name] = {"status": r.status_code, **shape(r.json())}
        seen[f"GET {gen}/datasets/{{name}}"] = {"by_name": by_name}
    return dict(sorted(seen.items()))


def build() -> dict:
    with tempfile.TemporaryDirectory(prefix="hist-manifest-") as tmp:
        routes = route_contract(create_app(str(Path(tmp) / "trialforge.db"), frontend_dir=str(Path(tmp) / "none")).openapi())
    observed = {}
    for p in sorted(FIXTURES.glob("*.json")):
        with tempfile.TemporaryDirectory(prefix="hist-manifest-") as tmp:
            observed[p.stem] = observe(p.stem, json.loads(p.read_text()), Path(tmp))
    return {"description": "Contract of /api/historical/* before the setup-lab restoration (generated by scripts/gen_historical_manifest.py)",
            "routes": routes, "observed": observed}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--check", action="store_true", help="compare with the file on disk instead of writing it")
    args = ap.parse_args()
    text = json.dumps(build(), indent=2, sort_keys=True) + "\n"
    out = Path(args.out)
    if args.check:
        same = out.exists() and out.read_text() == text
        print("historical route manifest is up to date" if same else "historical route manifest differs")
        return 0 if same else 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(f"wrote {out} ({len(json.loads(text)['routes'])} routes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
