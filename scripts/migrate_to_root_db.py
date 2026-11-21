"""Move every legacy store into the root database (data/trialforge.db), verify, and only then delete the legacy stores.

    python scripts/migrate_to_root_db.py --dry-run          # rehearse on a temporary copy; changes nothing
    python scripts/migrate_to_root_db.py                    # back up, migrate, verify (legacy files are kept)
    python scripts/migrate_to_root_db.py --delete-legacy    # ... and delete the legacy stores if verification passed

Legacy stores (defaults; override with the options):
  f1-setup-lab/data/f1lab.db              SDK experiment tables of F1 SETUP LAB
  wattwise-lab/data/wattwise.db           SDK experiment tables of WATTWISE LAB
  f1-setup-lab/data/historical/sessions/  FastF1 sessions as Parquet (needs pyarrow to read)
  f1-setup-lab/data/evidence/             stint evidence JSON + decoded telemetry .jsonl.gz
  f1-setup-lab/data/sessions/*/raw.f1raw  raw UDP captures
  wattwise-lab/data/tariffs.json          tariff library
The FastF1 download cache (f1-setup-lab/data/historical/fastf1-cache) is a library cache, not app data: with
--delete-legacy it is moved to data/fastf1-cache so nothing has to be downloaded again.
Browser localStorage cannot be read from here: WATTWISE uploads it once automatically the next time it is opened.

Re-running is safe: experiments/sessions/evidence already in the root database are skipped. Exit code 0 only when
verification passed.
"""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "f1-setup-lab" / "backend"), str(ROOT / "wattwise-lab" / "backend")]


from db import create_db_engine, migrate, resolve_db_url  # noqa: E402
from db.config import sqlite_file_path  # noqa: E402
from db.models.common import APP_F1, APP_WATTWISE  # noqa: E402

SDK_TABLES = ["experiments", "plans", "runs", "measurements", "processed_measurements", "analyses", "recommendations", "reports", "jobs", "state_log"]
AUTO_PK = {"analyses": "seq", "recommendations": "seq", "reports": "seq", "jobs": "job_id", "state_log": "seq"}


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    migrated: dict[str, int] = field(default_factory=dict)

    def say(self, s: str) -> None:
        self.lines.append(s)
        print(s, flush=True)

    def bad(self, s: str) -> None:
        self.mismatches.append(s)
        print("MISMATCH: " + s, flush=True)

    def count(self, k: str, n: int = 1) -> None:
        self.migrated[k] = self.migrated.get(k, 0) + n


# ------------------------------------------------------------------------------------------- SDK experiment tables
def _cols(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA {schema}.table_info("{table}")')]


def prepared_copy(legacy_db: Path, workdir: Path) -> Path:
    """A consistent copy of a legacy SDK database, upgraded to the SDK's current schema (the original is never modified)."""
    from trialforge import SQLiteExperimentRepository

    tmp = workdir / f"{legacy_db.stem}.prepared.db"
    src, dst = sqlite3.connect(legacy_db), sqlite3.connect(tmp)
    src.backup(dst)  # consistent even if the legacy app is still running
    src.close()
    dst.close()
    SQLiteExperimentRepository(f"sqlite:///{tmp}").engine.dispose()  # 0.1 snapshot / older schema -> current (copy only)
    return tmp


def migrate_sdk(prepared: Path, label: str, app: str, target: Path, rep: Report) -> None:
    """Copy one prepared legacy SDK database into the root database (one transaction), tagging experiments with ``app``."""
    conn = sqlite3.connect(target, timeout=30, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("ATTACH DATABASE ? AS legacy", (str(prepared),))
    legacy_tables = {r[0] for r in conn.execute("SELECT name FROM legacy.sqlite_master WHERE type='table'")}
    try:
        conn.execute("BEGIN IMMEDIATE")
        ids = [r[0] for r in conn.execute("SELECT experiment_id FROM legacy.experiments ORDER BY experiment_id")]
        owners = dict(conn.execute("SELECT experiment_id, app FROM main.app_experiment_owner"))
        new, skipped = [], []
        for i in ids:
            if owners.get(i) == app:
                skipped.append(i)  # already migrated (re-run)
            elif i in owners:
                rep.bad(f"{label}: experiment {i} already belongs to {owners[i]}")
            elif conn.execute("SELECT 1 FROM main.experiments WHERE experiment_id=?", (i,)).fetchone():
                rep.bad(f"{label}: experiment {i} exists in the root database without an owner")
            else:
                new.append(i)
        if new:
            conn.execute("CREATE TEMP TABLE mig_ids (experiment_id TEXT PRIMARY KEY)")
            conn.executemany("INSERT INTO mig_ids VALUES (?)", [(i,) for i in new])
            for t in [t for t in SDK_TABLES if t in legacy_tables]:
                cols = [c for c in _cols(conn, "legacy", t) if c != AUTO_PK.get(t) and c in _cols(conn, "main", t)]
                cl = ", ".join(f'"{c}"' for c in cols)
                order = f' ORDER BY "{AUTO_PK[t]}"' if t in AUTO_PK else ""  # keep the original order; new integer keys are assigned
                conn.execute(f'INSERT INTO main."{t}" ({cl}) SELECT {cl} FROM legacy."{t}" WHERE experiment_id IN (SELECT experiment_id FROM mig_ids){order}')
            if "adapters" in legacy_tables:
                conn.execute("INSERT OR IGNORE INTO main.adapters SELECT * FROM legacy.adapters")
            conn.executemany("INSERT INTO main.app_experiment_owner (experiment_id, app, created_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                             [(i, app) for i in new])
            conn.execute("DROP TABLE temp.mig_ids")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    rep.count(f"experiments ({app})", len(new))
    rep.say(f"  {len(new)} experiment(s) copied from {label}" + (f", {len(skipped)} already migrated" if skipped else ""))


def verify_sdk(prepared: Path, label: str, app: str, target: Path, rep: Report) -> None:
    conn = sqlite3.connect(target)
    conn.execute("ATTACH DATABASE ? AS legacy", (str(prepared),))
    try:
        legacy_tables = {r[0] for r in conn.execute("SELECT name FROM legacy.sqlite_master WHERE type='table'")}
        # every legacy row must be in the root database. Rows the apps added after an earlier migration run are fine.
        for t in [t for t in SDK_TABLES if t in legacy_tables]:
            n_old = conn.execute(f'SELECT COUNT(*) FROM legacy."{t}"').fetchone()[0]
            n_new = conn.execute(f'SELECT COUNT(*) FROM main."{t}" WHERE experiment_id IN (SELECT experiment_id FROM legacy.experiments)').fetchone()[0]
            if t in AUTO_PK:  # new integer keys were assigned: compare counts
                missing = max(0, n_old - n_new)
            else:
                pk = [r[1] for r in conn.execute(f'PRAGMA legacy.table_info("{t}")') if r[5]]
                on = " AND ".join(f'm."{c}" = l."{c}"' for c in pk)
                missing = conn.execute(f'SELECT COUNT(*) FROM legacy."{t}" l LEFT JOIN main."{t}" m ON {on} WHERE m."{pk[0]}" IS NULL').fetchone()[0]
            if missing:
                rep.bad(f"{label}.{t}: {missing} of {n_old} legacy rows are missing from the root database")
            else:
                rep.say(f"  ok {label}.{t}: {n_old} legacy rows present ({n_new} in the root database)")
        bad_owner = conn.execute("SELECT COUNT(*) FROM legacy.experiments e LEFT JOIN main.app_experiment_owner o ON o.experiment_id = e.experiment_id "
                                 "WHERE o.app IS NULL OR o.app != ?", (app,)).fetchone()[0]
        if bad_owner:
            rep.bad(f"{label}: {bad_owner} experiment(s) not owned by {app}")
    finally:
        conn.close()


# ------------------------------------------------------------------------------------------- F1 historical Parquet
def _read_parquet(p: Path):
    import pandas as pd

    try:
        return pd.read_parquet(p)
    except ImportError as exc:  # pyarrow is no longer an app dependency
        raise SystemExit("legacy Parquet sessions found: install pyarrow for the migration only (pip install pyarrow)") from exc


def legacy_session_frames(d: Path):
    import pandas as pd

    manifest = json.loads((d / "manifest.json").read_text())
    laps = _read_parquet(d / "laps.parquet") if (d / "laps.parquet").exists() else pd.DataFrame()
    weather = _read_parquet(d / "weather.parquet") if (d / "weather.parquet").exists() else pd.DataFrame()
    codes = {str(x.get("code", "")).upper(): str(x.get("code")) for x in manifest.get("drivers", [])}
    tel = {codes.get(p.stem.upper(), p.stem.upper()): _read_parquet(p) for p in sorted((d / "telemetry").glob("*.parquet"))}
    pos = {codes.get(p.stem.upper(), p.stem.upper()): _read_parquet(p) for p in sorted((d / "position").glob("*.parquet"))}
    return manifest, laps, weather, tel, pos


def migrate_historical(sessions_dir: Path, engine, rep: Report, verify_only: bool = False) -> None:
    from f1lab.integrations.historical.repository import HistoricalRepository

    repo = HistoricalRepository(engine, sessions_dir.parent / "fastf1-cache")
    for d in sorted(p for p in sessions_dir.iterdir() if p.is_dir()):
        if not (d / "manifest.json").exists():
            rep.say(f"  skipped {d.name}: no manifest.json (an import that never completed)")
            continue
        manifest, laps, weather, tel, pos = legacy_session_frames(d)
        sid = manifest.get("session_id") or d.name
        if not verify_only:
            if repo.exists(sid):
                rep.say(f"  session {sid} already in the root database, skipped")
            else:
                repo.save_session(sid, manifest, laps, weather, tel, pos)
                rep.count("historical sessions")
                rep.say(f"  session {sid}: {len(laps)} laps, {sum(len(x) for x in tel.values())} telemetry samples")
            continue
        if not repo.exists(sid):
            rep.bad(f"historical session {sid} missing from the root database")
            continue
        pairs = [("laps", laps, repo.laps(sid)), ("weather", weather, repo.weather(sid))]
        pairs += [(f"telemetry/{k}", v, repo.telemetry(sid, k)) for k, v in tel.items()]
        pairs += [(f"position/{k}", v, repo.position(sid, k)) for k, v in pos.items()]
        for name, old, new in pairs:
            if len(old) != len(new) or (len(old) and list(old.columns) != list(new.columns)):
                rep.bad(f"session {sid} {name}: {len(old)} legacy rows / {len(new)} stored, columns {list(old.columns)} vs {list(new.columns)}")
        rep.say(f"  ok session {sid}: {len(pairs)} frames")


# ------------------------------------------------------------------------------------------- F1 evidence + raw UDP
def migrate_evidence(evidence_dir: Path, engine, rep: Report, verify_only: bool = False) -> None:
    from f1lab.integrations.game_udp.evidence import load_evidence, save_evidence, save_telemetry_series, telemetry_series_count

    for p in sorted(evidence_dir.glob("*/*.json")):
        if p.name.endswith(".telemetry.jsonl.gz"):
            continue
        exp, run = p.parent.name, p.stem
        payload = json.loads(p.read_text())
        series = p.with_suffix(".telemetry.jsonl.gz")
        records = []
        if series.exists():
            with gzip.open(series, "rt", encoding="utf-8") as fh:
                records = [json.loads(line) for line in fh if line.strip()]
        if verify_only:
            stored = load_evidence(engine, exp, run)
            # telemetry_series pointed at the old .jsonl.gz file and now points at the table: compare everything else
            same = stored is not None and {k: v for k, v in stored.items() if k != "telemetry_series"} == \
                {k: v for k, v in json.loads(json.dumps(payload, default=str)).items() if k != "telemetry_series"}
            if not same:
                rep.bad(f"evidence {exp}/{run} differs in the root database")
            if telemetry_series_count(engine, exp, run) != len(records):
                rep.bad(f"evidence {exp}/{run}: {len(records)} telemetry samples, {telemetry_series_count(engine, exp, run)} stored")
            continue
        if load_evidence(engine, exp, run) is not None:
            rep.say(f"  evidence {exp}/{run} already migrated, skipped")
            continue
        if records:
            save_telemetry_series(engine, exp, run, records)
            payload["telemetry_series"] = {"table": "f1_stint_telemetry", "samples": len(records), "migrated_from": series.name}
        save_evidence(engine, exp, run, payload)
        rep.count("stint evidence")
    if verify_only:
        rep.say(f"  ok evidence: {len(list(evidence_dir.glob('*/*.json')))} file(s) checked")


def migrate_raw(sessions_dir: Path, engine, rep: Report, verify_only: bool = False) -> None:
    from f1lab.integrations.game_udp.recorder import import_raw_file, read_raw, read_raw_file

    for p in sorted(sessions_dir.glob("*/raw.f1raw")):
        rid = p.parent.name
        n_file = sum(1 for _ in read_raw_file(p))
        n_db = sum(1 for _ in read_raw(engine, rid))
        if verify_only:
            if n_file != n_db:
                rep.bad(f"raw recording {rid}: {n_file} packets in the file, {n_db} stored")
            else:
                rep.say(f"  ok raw recording {rid}: {n_file} packets")
            continue
        if n_db:
            rep.say(f"  raw recording {rid} already migrated, skipped")
            continue
        import_raw_file(engine, p, rid, note="migrated from legacy .f1raw")
        rep.count("raw UDP recordings")


# ------------------------------------------------------------------------------------------- WATTWISE tariffs.json
def migrate_tariffs(path: Path, engine, rep: Report, verify_only: bool = False) -> None:
    from wattwise.adapters.household_model import DEFAULT_TARIFFS
    from wattwise.services.tariffs import TariffStore

    legacy = json.loads(path.read_text())
    if verify_only:
        stored = {t["id"]: t for t in TariffStore(engine).list()}
        for t in legacy:
            if stored.get(t["id"], {}).get("periods") != t.get("periods") or stored[t["id"]].get("name") != t.get("name"):
                rep.bad(f"tariff {t.get('id')} differs or is missing in the root database")
        rep.say(f"  ok tariffs: {len(legacy)} checked")
        return
    store = TariffStore(engine)
    # the legacy file is the user's whole library: it replaces the seeded examples, unless tariffs were already edited here
    current = store.list()
    untouched = all(t in DEFAULT_TARIFFS for t in current)
    n = store.import_records(legacy, replace=untouched)
    rep.count("tariffs", n)
    rep.say(f"  {n} tariff(s) from {path}" + ("" if untouched else " (merged into an already edited library)"))


# ------------------------------------------------------------------------------------------- main
@dataclass
class Legacy:
    f1_db: Path
    ww_db: Path
    f1_data: Path
    tariffs: Path

    @property
    def hist_sessions(self) -> Path:
        return self.f1_data / "historical" / "sessions"

    @property
    def ff1_cache(self) -> Path:
        return self.f1_data / "historical" / "fastf1-cache"

    @property
    def evidence(self) -> Path:
        return self.f1_data / "evidence"

    @property
    def raw_sessions(self) -> Path:
        return self.f1_data / "sessions"

    def stores(self) -> list[tuple[str, Path]]:
        out = []
        for name, p in (("F1 SDK database", self.f1_db), ("WATTWISE SDK database", self.ww_db), ("F1 historical Parquet", self.hist_sessions),
                        ("F1 stint evidence", self.evidence), ("F1 raw UDP captures", self.raw_sessions), ("WATTWISE tariffs.json", self.tariffs)):
            if p.exists():
                out.append((name, p))
        return out


def run_all(legacy: Legacy, target: Path, rep: Report, verify_only: bool) -> None:
    engine = create_db_engine(target)
    migrate(engine)
    from trialforge import SQLiteExperimentRepository

    SQLiteExperimentRepository(f"sqlite:///{target}").engine.dispose()  # SDK tables exist in the root database
    with tempfile.TemporaryDirectory() as td:
        for db_path, app in ((legacy.f1_db, APP_F1), (legacy.ww_db, APP_WATTWISE)):
            if db_path.exists():
                rep.say(f"{'verify' if verify_only else 'migrate'} {db_path}")
                prepared = prepared_copy(db_path, Path(td))
                (verify_sdk if verify_only else migrate_sdk)(prepared, db_path.name, app, target, rep)
    for p, fn in ((legacy.hist_sessions, migrate_historical), (legacy.evidence, migrate_evidence), (legacy.raw_sessions, migrate_raw),
                  (legacy.tariffs, migrate_tariffs)):
        if p.exists():
            rep.say(f"{'verify' if verify_only else 'migrate'} {p}")
            fn(p, engine, rep, verify_only)
    engine.dispose()


def backup(legacy: Legacy, target: Path, backup_root: Path, rep: Report) -> Path:
    dest = backup_root / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest.mkdir(parents=True, exist_ok=False)
    for name, p in legacy.stores():
        to = dest / name.lower().replace(" ", "-").replace(".", "-") / p.name  # one folder per store (two are called 'sessions')
        to.parent.mkdir(parents=True, exist_ok=True)
        if p.suffix == ".db":
            src, dst = sqlite3.connect(p), sqlite3.connect(to)
            src.backup(dst)
            src.close()
            dst.close()
        elif p.is_dir():
            shutil.copytree(p, to)
        else:
            shutil.copy2(p, to)
    if target.exists():
        src, dst = sqlite3.connect(target), sqlite3.connect(dest / "trialforge.before-migration.db")
        src.backup(dst)
        src.close()
        dst.close()
    rep.say(f"backup written to {dest}")
    return dest


def delete_legacy(legacy: Legacy, target: Path, rep: Report) -> None:
    for name, p in legacy.stores():
        if p.suffix == ".db":
            for q in (p, p.with_name(p.name + "-wal"), p.with_name(p.name + "-shm")):
                q.unlink(missing_ok=True)
        elif p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        rep.say(f"deleted {name}: {p}")
    if legacy.ff1_cache.exists():  # a download cache, not app data: keep it where the app now looks for it
        new_cache = target.parent / "fastf1-cache"
        if new_cache.exists():
            shutil.rmtree(legacy.ff1_cache)
            rep.say(f"removed the old FastF1 cache {legacy.ff1_cache} ({new_cache} already exists)")
        else:
            shutil.move(str(legacy.ff1_cache), str(new_cache))
            rep.say(f"moved the FastF1 download cache to {new_cache}")
    hist = legacy.f1_data / "historical"
    if hist.exists() and not any(hist.iterdir()):
        hist.rmdir()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", default=None, help="root database (default: TRIALFORGE_DB or <repo>/data/trialforge.db)")
    ap.add_argument("--f1-db", default=str(ROOT / "f1-setup-lab" / "data" / "f1lab.db"))
    ap.add_argument("--ww-db", default=str(ROOT / "wattwise-lab" / "data" / "wattwise.db"))
    ap.add_argument("--f1-data", default=str(ROOT / "f1-setup-lab" / "data"), help="F1 data dir holding historical/, evidence/, sessions/")
    ap.add_argument("--tariffs", default=str(ROOT / "wattwise-lab" / "data" / "tariffs.json"))
    ap.add_argument("--backup-dir", default=None, help="default: <target dir>/backups")
    ap.add_argument("--dry-run", action="store_true", help="rehearse on a temporary copy of the root database; change nothing")
    ap.add_argument("--delete-legacy", action="store_true", help="delete the legacy stores after a successful verification")
    a = ap.parse_args(argv)
    if a.dry_run and a.delete_legacy:
        ap.error("--dry-run and --delete-legacy cannot be combined")
    target = sqlite_file_path(resolve_db_url(a.target))
    if target is None:
        ap.error("the root database must be a SQLite file")
    legacy = Legacy(Path(a.f1_db), Path(a.ww_db), Path(a.f1_data), Path(a.tariffs))
    rep = Report()
    stores = legacy.stores()
    rep.say(f"root database: {target}")
    if not stores:
        rep.say("no legacy stores found: nothing to migrate")
        return 0
    for name, p in stores:
        rep.say(f"found {name}: {p}")
    if a.dry_run:
        with tempfile.TemporaryDirectory() as td:
            rehearsal = Path(td) / "rehearsal.db"
            if target.exists():
                src, dst = sqlite3.connect(target), sqlite3.connect(rehearsal)
                src.backup(dst)
                src.close()
                dst.close()
            run_all(legacy, rehearsal, rep, verify_only=False)
            run_all(legacy, rehearsal, rep, verify_only=True)
        rep.say(f"\nDRY RUN: would migrate {rep.migrated or 'nothing new'}; {len(rep.mismatches)} mismatch(es). Nothing was changed.")
        return 0 if not rep.mismatches else 1
    backup(legacy, target, Path(a.backup_dir) if a.backup_dir else target.parent / "backups", rep)
    run_all(legacy, target, rep, verify_only=False)
    rep.say("\nverifying ...")
    run_all(legacy, target, rep, verify_only=True)
    if rep.mismatches:
        rep.say(f"\nVERIFICATION FAILED ({len(rep.mismatches)} mismatch(es)); legacy stores were NOT deleted.")
        return 1
    rep.say(f"\nmigrated {rep.migrated or 'nothing new'}; verification passed.")
    if a.delete_legacy:
        delete_legacy(legacy, target, rep)
    else:
        rep.say("legacy stores kept; run again with --delete-legacy to remove them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
