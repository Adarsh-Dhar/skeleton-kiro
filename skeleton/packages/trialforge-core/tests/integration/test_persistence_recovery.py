import asyncio
import sqlite3

import pytest
from helpers import ScriptedAdapter, basic_spec
from sqlalchemy import text

from trialforge import (
    AdapterCapabilities,
    AdapterRegistry,
    AnalysisEngine,
    ExecutionError,
    ExecutionPolicy,
    ExperimentResult,
    ExperimentService,
    ExperimentStatus,
    Factor,
    Measurement,
    PersistenceError,
    Run,
    RunStatus,
    SQLiteExperimentRepository,
)
from trialforge.persistence.repository import SCHEMA_VERSION


def make(adapter, url, **kw):
    reg = AdapterRegistry()
    reg.register(adapter)
    return ExperimentService(registry=reg, repository=SQLiteExperimentRepository(url), **kw)


class HangOnX1(ScriptedAdapter):
    async def execute_run(self, ctx):
        if ctx.configuration["x"] == 1:
            self.calls[ctx.run_id] = self.calls.get(ctx.run_id, 0) + 1
            await asyncio.sleep(3600)
        return await super().execute_run(ctx)


async def crash_midway(url, spec, **service_kw):
    """Start executing; hang on run x==1; kill the 'process' (task cancellation) while it is RUNNING."""
    a = HangOnX1("ad", caps=AdapterCapabilities(concurrent=False))
    s = make(a, url, **service_kw)
    plan = s.design(spec)
    t = asyncio.create_task(s.execute(plan, spec))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if any(r.status == RunStatus.RUNNING for r in s.repository.list_runs(spec.experiment_id)):
            break
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    s.repository.engine.dispose()


# ------------------------------------------------------------------------- restart survival
async def test_completed_runs_and_analysis_survive_process_restart(tmp_path):
    url = f"sqlite:///{tmp_path / 'a.db'}"
    spec = basic_spec(3)
    s1 = make(ScriptedAdapter("ad"), url)
    res1 = await s1.run(spec)
    an1 = s1.analyse(res1)
    s1.repository.engine.dispose()
    s2 = make(ScriptedAdapter("ad"), url)  # "new process": nothing cached
    res2 = s2.get(spec.experiment_id)
    assert res2.status == ExperimentStatus.COMPLETED and [r.run_id for r in res2.runs] == [r.run_id for r in res1.runs]
    assert [r.configuration for r in res2.runs] == [r.configuration for r in res1.runs]
    assert [m.model_dump() for m in res2.runs[0].measurements] == [m.model_dump() for m in res1.runs[0].measurements]
    assert [p.model_dump() for p in res2.runs[0].processed_measurements] == [
        p.model_dump() for p in res1.runs[0].processed_measurements
    ]
    an2 = AnalysisEngine().analyse(res2)  # reproduced from stored raw data only
    assert an2.model_dump(exclude={"created_at"}) == an1.model_dump(exclude={"created_at"})
    assert s2.repository.list_analyses(spec.experiment_id)[0].model_dump(exclude={"created_at"}) == an1.model_dump(
        exclude={"created_at"}
    )
    assert s2.list_experiments() == [spec.experiment_id]


async def test_historical_configurations_and_failed_runs_are_preserved_and_labelled(tmp_path):
    url = f"sqlite:///{tmp_path / 'h.db'}"
    a = ScriptedAdapter("ad", fail_first=1)
    s = make(a, url)
    spec = basic_spec(3)
    res = await s.run(spec)
    s2 = make(ScriptedAdapter("ad"), url)
    hist = s2.history(spec.experiment_id)
    cfgs = hist.configurations()
    assert [c["configuration"] for c in cfgs] == [{"x": r.configuration["x"]} for r in sorted(res.runs, key=lambda r: r.order)]
    assert {c["status"] for c in cfgs} == {"failed"} and all(c["outcomes"] == {} and "transient" in c["error"] for c in cfgs)
    assert hist.runs(RunStatus.COMPLETED) == []  # a failed run is never mistaken for success
    assert AnalysisEngine().analyse(s2.get(spec.experiment_id)).excluded_runs[0]["reason"].startswith("run status is failed")


# ------------------------------------------------------------------------------ crash recovery
@pytest.mark.parametrize("variant", ["resumable", "idempotent", "unsafe", "policy_fail"])
async def test_interrupted_runs_are_recovered_only_when_adapter_capabilities_allow(tmp_path, variant):
    url = f"sqlite:///{tmp_path / 'c.db'}"
    spec = basic_spec(3, execution=ExecutionPolicy(recovery="fail" if variant == "policy_fail" else "auto", max_concurrency=1))
    await crash_midway(url, spec)
    caps = {
        "resumable": AdapterCapabilities(resumable=True),
        "idempotent": AdapterCapabilities(idempotent=True),
        "unsafe": AdapterCapabilities(),
        "policy_fail": AdapterCapabilities(resumable=True, idempotent=True),
    }[variant]
    b = ScriptedAdapter("ad", caps=caps)
    s = make(b, url)
    before = {r.order: r for r in s.repository.list_runs(spec.experiment_id)}
    stuck = [r for r in before.values() if r.status == RunStatus.RUNNING]
    assert len(stuck) == 1 and stuck[0].configuration["x"] == 1  # honest persisted state after the crash
    done_before = [r for r in before.values() if r.status == RunStatus.COMPLETED]
    b.ready.add(stuck[0].run_id)
    actions = await s.recover(spec.experiment_id)
    assert len(actions) == 1 and actions[0]["run_id"] == stuck[0].run_id
    run = s.repository.get_run(stuck[0].run_id)
    if variant == "resumable":
        assert actions[0]["action"] == "resume" and run.status == RunStatus.COMPLETED and run.measurements[0].value == 42.0
        assert not b.calls  # resumed WITHOUT re-executing
    elif variant == "idempotent":
        assert actions[0]["action"] == "retry" and run.status == RunStatus.QUEUED
        res = await s.run_pending(spec.experiment_id)
        assert b.calls[stuck[0].run_id] == 1 and {r.status for r in res.runs} == {RunStatus.COMPLETED}
        assert len([r for r in res.runs if r.run_id == stuck[0].run_id]) == 1 and len(res.runs) == 3
    else:
        assert (
            actions[0]["action"] == "fail" and run.status == RunStatus.FAILED and run.error.startswith("interrupted by restart")
        )
        if variant == "unsafe":
            assert "retry explicitly with retry_run()" in run.error
        else:
            assert actions[0]["reason"] == "recovery policy is 'fail'"  # applies even to capable adapters
            assert "recovery policy is 'fail'" in run.error
        assert not b.calls
        s.retry_run(spec.experiment_id, run.run_id)  # explicit retry is still possible ...
        res = await s.run_pending(spec.experiment_id)
        assert {r.run_id: r.status for r in res.runs}[run.run_id] == RunStatus.COMPLETED and b.calls[run.run_id] == 1
    after = {r.run_id: r for r in s.repository.list_runs(spec.experiment_id)}
    for r in done_before:  # finished work is untouched
        assert after[r.run_id].status == RunStatus.COMPLETED and after[r.run_id].measurements == r.measurements
    assert len(after) == 3  # nothing duplicated


async def test_recovery_of_waiting_runs_depends_on_resumability(tmp_path):
    url = f"sqlite:///{tmp_path / 'w.db'}"
    spec = basic_spec(2)
    a = ScriptedAdapter("ad", delayed=True, caps=AdapterCapabilities(delayed_measurements=True, immediate_execution=False))
    s = make(a, url)
    res = await s.run(spec)
    assert {r.status for r in res.runs} == {RunStatus.WAITING_FOR_MEASUREMENTS}
    s.repository.engine.dispose()
    non = make(ScriptedAdapter("ad", caps=AdapterCapabilities(delayed_measurements=True, immediate_execution=False)), url)
    acts = await non.recover(spec.experiment_id)
    assert {x["action"] for x in acts} == {"fail"} and "cannot resume" in acts[0]["reason"]
    # resumable adapter: status() consulted via stored handle, then collect_result
    url2 = f"sqlite:///{tmp_path / 'w2.db'}"
    spec2 = basic_spec(2)
    s = make(
        ScriptedAdapter(
            "ad", delayed=True, caps=AdapterCapabilities(delayed_measurements=True, immediate_execution=False, resumable=True)
        ),
        url2,
    )
    res = await s.run(spec2)
    s.repository.engine.dispose()
    rb = ScriptedAdapter("ad", caps=AdapterCapabilities(delayed_measurements=True, immediate_execution=False, resumable=True))
    rb.ready.add(res.runs[0].run_id)
    s2 = make(rb, url2)
    acts = {a["run_id"]: a for a in await s2.recover(spec2.experiment_id)}
    st = {r.run_id: r.status for r in s2.get(spec2.experiment_id).runs}
    assert st[res.runs[0].run_id] == RunStatus.COMPLETED and st[res.runs[1].run_id] == RunStatus.WAITING_FOR_MEASUREMENTS
    assert acts[res.runs[1].run_id]["action"] == "resume"


async def test_queued_work_is_durable_and_resumes_after_restart(tmp_path):
    url = f"sqlite:///{tmp_path / 'q.db'}"
    spec = basic_spec(4)
    s1 = make(ScriptedAdapter("ad"), url)
    plan = s1.design(spec)
    s1.repository.enqueue(spec.experiment_id, plan.runs[0].run_id)
    assert s1.repository.pending_jobs(spec.experiment_id)
    s1.repository.engine.dispose()
    a = ScriptedAdapter("ad")
    s2 = make(a, url)
    res = await s2.run_pending(spec.experiment_id)  # spec comes from the database; no in-memory cache
    assert res.status == ExperimentStatus.COMPLETED and len(a.calls) == 4
    assert s2.repository.pending_jobs(spec.experiment_id) == []


# ------------------------------------------------------------------------- spec retrieval (old _specs defect)
async def test_execute_plan_without_spec_uses_repository_not_a_hidden_cache(tmp_path):
    url = f"sqlite:///{tmp_path / 's.db'}"
    spec = basic_spec(2)
    s1 = make(ScriptedAdapter("ad"), url)
    plan = s1.design(spec)
    s1.repository.engine.dispose()
    s2 = make(ScriptedAdapter("ad"), url)
    assert s2._spec_overrides == {}
    res = await s2.execute(plan)  # no spec argument, fresh service
    assert res.status == ExperimentStatus.COMPLETED and res.spec.experiment_id == spec.experiment_id
    with pytest.raises(ExecutionError, match="no specification available"):
        s2.get_spec("does-not-exist")


async def test_callable_validators_are_not_persisted_and_the_loss_is_explicit(tmp_path):
    url = f"sqlite:///{tmp_path / 'v.db'}"
    spec = basic_spec(3).model_copy(
        update={"factors": [Factor(name="x", factor_type="discrete", values=[0, 1, 2], validation_rule=lambda v: v != 1)]}
    )
    s1 = make(ScriptedAdapter("ad"), url)
    s1.design(spec)
    s2 = make(ScriptedAdapter("ad"), url)
    restored = s2.get_spec(spec.experiment_id)
    assert restored.metadata["_callables_not_restored"] == ["factor:x"] and restored.factors[0].validation_rule is None
    s2.attach_spec(spec)
    assert s2.get_spec(spec.experiment_id).factors[0].validation_rule is not None


def test_spec_of_an_experiment_with_runs_cannot_be_changed(tmp_path):
    s = make(ScriptedAdapter("ad"), f"sqlite:///{tmp_path / 'i.db'}")
    spec = basic_spec(2)
    s.design(spec)
    with pytest.raises(PersistenceError, match="already has runs"):
        s.repository.save_experiment(spec.model_copy(update={"hypothesis": "changed after the fact"}))


# ---------------------------------------------------------------------------- transactions
def test_run_and_measurements_are_committed_atomically(tmp_path, monkeypatch):
    repo = SQLiteExperimentRepository(f"sqlite:///{tmp_path / 't.db'}")
    spec = basic_spec(1)
    repo.save_experiment(spec)
    run = Run(experiment_id=spec.experiment_id, configuration={"x": 0}, order=0, status=RunStatus.RUNNING)
    repo.save_run(run)
    run.measurements = [Measurement(run_id=run.run_id, metric="y", value=1.0, unit="u")]
    run.transition(RunStatus.COMPLETED)
    original = SQLiteExperimentRepository._write_run

    def exploding(session, r):
        original(session, r)  # everything written inside the open transaction ...
        raise RuntimeError("disk full")  # ... then the commit fails

    monkeypatch.setattr(SQLiteExperimentRepository, "_write_run", staticmethod(exploding))
    with pytest.raises(RuntimeError):
        repo.save_run(run)
    stored = repo.get_run(run.run_id)
    assert stored.status == RunStatus.RUNNING and stored.measurements == [] and stored.processed_measurements == []


async def test_failed_commit_never_leaves_a_run_marked_completed(tmp_path, monkeypatch):
    s = make(ScriptedAdapter("ad"), f"sqlite:///{tmp_path / 'f.db'}")
    original = SQLiteExperimentRepository._write_run

    def exploding(session, r):
        original(session, r)
        if r.status == RunStatus.COMPLETED:
            raise RuntimeError("injected commit failure")

    monkeypatch.setattr(SQLiteExperimentRepository, "_write_run", staticmethod(exploding))
    spec = basic_spec(2)
    res = await s.run(spec)
    assert RunStatus.COMPLETED not in {r.status for r in res.runs}
    assert {r.status for r in res.runs} == {RunStatus.FAILED} and "injected commit failure" in res.runs[0].error
    assert s.repository.state_log(spec.experiment_id, res.runs[0].run_id)[-1]["to"] == "failed"


# --------------------------------------------------------------------------------- migration
def test_legacy_snapshot_database_is_migrated_not_lost(tmp_path):
    from trialforge import DesignPlan

    spec = basic_spec(2)
    run = Run(
        experiment_id=spec.experiment_id,
        configuration={"x": 0},
        order=0,
        status=RunStatus.COMPLETED,
        measurements=[Measurement(run_id="r", metric="y", value=3.5, unit="u")],
    )
    plan = DesignPlan(
        experiment_id=spec.experiment_id, method="factorial", runs=[run], control_configuration={"x": 0}, random_seed=0
    )
    result = ExperimentResult(spec=spec, plan=plan, runs=[run], status=ExperimentStatus.COMPLETED)
    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE experiments (experiment_id VARCHAR PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT)")
    con.execute("INSERT INTO experiments VALUES (?, ?, ?)", (spec.experiment_id, result.model_dump_json(), "2026-01-01"))
    con.commit()
    con.close()
    repo = SQLiteExperimentRepository(f"sqlite:///{path}")
    assert repo.schema_version() == SCHEMA_VERSION
    got = repo.get(spec.experiment_id)
    assert got.status == ExperimentStatus.COMPLETED and got.runs[0].measurements[0].value == 3.5
    with repo.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM experiments_legacy_v1")).scalar() == 1  # original kept
    SQLiteExperimentRepository(f"sqlite:///{path}")  # idempotent re-open
    assert repo.list_ids() == [spec.experiment_id]


def test_snapshot_api_compatible_with_0_1(tmp_path):
    from conftest import build_result, simple_spec

    s = simple_spec([Factor(name="a", factor_type="discrete", values=[0, 1])])
    res = build_result(s, [({"a": 0}, 1.0), ({"a": 1}, 2.0)])
    repo = SQLiteExperimentRepository(f"sqlite:///{tmp_path / 'snap.db'}")
    repo.save(res)
    repo.save(res)  # saving twice is an update, not a duplicate
    got = repo.get(s.experiment_id)
    assert len(got.runs) == 2 and got.runs[1].processed_measurements[0].value == 2.0 and repo.list_ids() == [s.experiment_id]
    with pytest.raises(KeyError):
        repo.get("nope")
