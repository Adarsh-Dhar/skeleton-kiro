"""Move every legacy store into the root database (data/trialforge.db), verify, and only then delete the legacy stores.

    python scripts/migrate_to_root_db.py --dry-run          # rehearse on a temporary copy; changes nothing
    python scripts/migrate_to_root_db.py                    # back up, migrate, verify (legacy files are kept)
    python scripts/migrate_to_root_db.py --delete-legacy    # ... and delete the legacy stores if verification passed

Legacy stores (defaults; override with the options):
  wattwise-lab/data/wattwise.db           SDK experiment tables of WATTWISE LAB
  wattwise-lab/data/tariffs.json          tariff library

F1 SETUP LAB has no legacy store to migrate: it keeps only OpenF1 historical sessions, which are imported straight into the
root database.

Browser localStorage cannot be read from here: WATTWISE uploads it once automatically the next time it is opened.

Re-running is safe: experiments and tariffs already in the root database are skipped. Exit code 0 only when
verification passed.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "wattwise-lab" / "backend")]


from db import create_db_engine, migrate, resolve_db_url  # noqa: E402
from db.config import sqlite_file_path  # noqa: E402
from db.models.common import APP_WATTWISE  # noqa: E402

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


# ------------------------------------------------------------------------------------------- WATTWISE tariffs.json
def migrate_tariffs(path: Path, engine, rep: Report, verify_only: bool = False) -> None:
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
    # the legacy file is the user's whole library: it replaces an empty library or one holding only the old built-in examples
    # (recognised by their content and labelled source_type "example"), unless tariffs were already added or edited here
    current = store.list()
    untouched = all((t.get("source") or {}).get("source_type") == "example" for t in current)
    n = store.import_records(legacy, replace=untouched)
    rep.count("tariffs", n)
    rep.say(f"  {n} tariff(s) from {path}" + ("" if untouched else " (merged into an already edited library)"))


# ------------------------------------------------------------------------------------------- main
@dataclass
class Legacy:
    ww_db: Path
    tariffs: Path

    def stores(self) -> list[tuple[str, Path]]:
        return [(name, p) for name, p in (("WATTWISE SDK database", self.ww_db), ("WATTWISE tariffs.json", self.tariffs)) if p.exists()]


def run_all(legacy: Legacy, target: Path, rep: Report, verify_only: bool) -> None:
    engine = create_db_engine(target)
    migrate(engine)
    from trialforge import SQLiteExperimentRepository

    SQLiteExperimentRepository(f"sqlite:///{target}").engine.dispose()  # SDK tables exist in the root database
    with tempfile.TemporaryDirectory() as td:
        for db_path, app in ((legacy.ww_db, APP_WATTWISE),):
            if db_path.exists():
                rep.say(f"{'verify' if verify_only else 'migrate'} {db_path}")
                prepared = prepared_copy(db_path, Path(td))
                (verify_sdk if verify_only else migrate_sdk)(prepared, db_path.name, app, target, rep)
    for p, fn in ((legacy.tariffs, migrate_tariffs),):
        if p.exists():
            rep.say(f"{'verify' if verify_only else 'migrate'} {p}")
            fn(p, engine, rep, verify_only)
    engine.dispose()


def backup(legacy: Legacy, target: Path, backup_root: Path, rep: Report) -> Path:
    dest = backup_root / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest.mkdir(parents=True, exist_ok=False)
    for name, p in legacy.stores():
        to = dest / name.lower().replace(" ", "-").replace(".", "-") / p.name  # one folder per store
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", default=None, help="root database (default: TRIALFORGE_DB or <repo>/data/trialforge.db)")
    ap.add_argument("--ww-db", default=str(ROOT / "wattwise-lab" / "data" / "wattwise.db"))
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
    legacy = Legacy(Path(a.ww_db), Path(a.tariffs))
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
