import asyncio

import pytest
from helpers import LegacyAdapter, ScriptedAdapter, basic_spec

from trialforge import (
    AdapterCapabilities,
    AdapterRegistry,
    AdapterUnavailableError,
    CapabilityError,
    EventBus,
    ExecutionError,
    ExecutionPolicy,
    ExperimentService,
    ExperimentStatus,
    InvalidTransitionError,
    LocalBackgroundRunner,
    MeasurementDefinition,
    RunStatus,
    ValidationError,
)


def svc(*adapters, **kw):
    reg = AdapterRegistry()
    for a in adapters:
        reg.register(a)
    return ExperimentService(registry=reg, **kw)


# --------------------------------------------------------------------------- adapter selection
async def test_multiple_adapters_are_selected_per_experiment():
    a, b = ScriptedAdapter("alpha"), ScriptedAdapter("beta")
    s = svc(a, b)
    ra = await s.run(basic_spec(2, adapter_name="alpha"))
    rb = await s.run(basic_spec(3, adapter_name="beta"))
    assert {r.adapter_name for r in ra.runs} == {"alpha"} and {r.adapter_name for r in rb.runs} == {"beta"}
    assert len(a.calls) == 2 and len(b.calls) == 3
    assert [m.name for m in s.registry.list()] == ["alpha", "beta"]
    assert {x["name"] for x in s.repository.list_adapters()} == {"alpha", "beta"}


async def test_ambiguous_missing_and_unregistered_adapter_selection_fail_clearly():
    s = svc(ScriptedAdapter("alpha"), ScriptedAdapter("beta"))
    plan = s.design(basic_spec())  # designing without executing is allowed
    with pytest.raises(AdapterUnavailableError, match="several are registered"):
        await s.execute(plan)
    with pytest.raises(ValidationError, match="not registered"):
        s.design(basic_spec(adapter_name="gamma"))
    with pytest.raises(AdapterUnavailableError):
        ExperimentService().registry.select(basic_spec())
    with pytest.raises(ValueError, match="already registered"):
        s.registry.register(ScriptedAdapter("alpha"))
    s.registry.unregister("beta")
    assert (await s.run(basic_spec(1))).status == ExperimentStatus.COMPLETED  # sole adapter selected implicitly


async def test_registry_rejects_non_adapters_and_checks_capabilities():
    reg = AdapterRegistry()
    with pytest.raises(Exception, match="ExperimentAdapter protocol"):
        reg.register(object())
    with pytest.raises(Exception, match="neither immediate nor delayed"):
        reg.register(ScriptedAdapter("x", caps=AdapterCapabilities(immediate_execution=False)))
    ts_spec = basic_spec(metadata={}).model_copy(
        update={"measurement_definitions": [MeasurementDefinition(metric="y", unit="u", time_series=True)]}
    )
    s = svc(ScriptedAdapter("plain"))
    with pytest.raises(ValidationError, match="lacks required capability 'time_series'"):
        s.design(ts_spec)
    with pytest.raises(CapabilityError):
        reg.check_capabilities(ScriptedAdapter("p"), ts_spec)


async def test_adapter_availability_is_checked_and_lifecycle_hooks_run():
    class Flaky(ScriptedAdapter):
        up = False
        started = stopped = 0

        async def available(self):
            return self.up

        async def startup(self):
            Flaky.started += 1

        async def shutdown(self):
            Flaky.stopped += 1

    a = Flaky("flaky")
    s = svc(a)
    plan = s.design(basic_spec(2))
    with pytest.raises(AdapterUnavailableError):
        await s.execute(plan)
    Flaky.up = True
    await s.execute(plan)
    await s.registry.shutdown_all()
    assert Flaky.started == 1 and Flaky.stopped == 1


# ----------------------------------------------------------------------------- state & events
async def test_state_transitions_are_persisted_ordered_and_published():
    events = EventBus()
    seen = []
    events.subscribe("", lambda e: seen.append(e.name))
    events.subscribe("run.", lambda e: 1 / 0)  # a failing observer must not break execution
    s = svc(ScriptedAdapter("a"), events=events)
    spec = basic_spec(2, adapter_name="a")
    result = await s.run(spec)
    log = s.repository.state_log(spec.experiment_id)
    exp_states = [e["to"] for e in log if e["run_id"] is None]
    assert exp_states[:4] == ["validated", "designed", "queued", "running"] and exp_states[-1] == "completed"
    run_id = result.runs[0].run_id
    assert [e["to"] for e in s.repository.state_log(spec.experiment_id, run_id)] == ["queued", "running", "completed"]
    assert result.runs[0].history[-1]["reason"] == "measurements committed"
    assert "experiment.designed" in seen and seen.count("run.completed") == 2


def test_experiment_status_cannot_jump_illegally():
    s = svc(ScriptedAdapter("a"))
    spec = basic_spec(1)
    s.design(spec)
    with pytest.raises(InvalidTransitionError):
        s._advance_experiment(spec.experiment_id, ExperimentStatus.CREATED)
    s.repository.set_status(spec.experiment_id, ExperimentStatus.CANCELLED)
    with pytest.raises(InvalidTransitionError):
        s._advance_experiment(spec.experiment_id, ExperimentStatus.RUNNING)


# ------------------------------------------------------------- retries, failure, idempotency
async def test_retry_policy_recovers_transient_failures_and_counts_attempts():
    a = ScriptedAdapter("a", fail_first=2)
    s = svc(a)
    res = await s.run(basic_spec(2, execution=ExecutionPolicy(max_retries=2)))
    assert res.status == ExperimentStatus.COMPLETED and all(r.attempts == 3 for r in res.runs)
    assert [h.get("event") for h in res.runs[0].history if h.get("event")] == ["retry", "retry"]


async def test_exhausted_retries_fail_and_failed_runs_never_become_successful_implicitly():
    a = ScriptedAdapter("a", fail_first=3)
    s = svc(a)
    spec = basic_spec(2, execution=ExecutionPolicy(max_retries=1))
    res = await s.run(spec)
    assert res.status == ExperimentStatus.FAILED and {r.status for r in res.runs} == {RunStatus.FAILED}
    assert all("transient failure" in r.error and r.attempts == 2 for r in res.runs)
    before = dict(a.calls)
    again = await s.run_pending(spec.experiment_id)
    assert a.calls == before and {r.status for r in again.runs} == {RunStatus.FAILED}  # not silently retried
    a.fail_first = 0
    s.retry_run(spec.experiment_id, res.runs[0].run_id)
    final = await s.run_pending(spec.experiment_id)
    st = {r.run_id: r.status for r in final.runs}
    assert st[res.runs[0].run_id] == RunStatus.COMPLETED and st[res.runs[1].run_id] == RunStatus.FAILED
    assert final.status == ExperimentStatus.FAILED
    with pytest.raises(InvalidTransitionError):
        s.retry_run(spec.experiment_id, res.runs[0].run_id)  # completed: not retryable


async def test_non_retryable_adapter_is_executed_exactly_once():
    a = ScriptedAdapter("a", always_fail=True, caps=AdapterCapabilities(retryable=False))
    res = await svc(a).run(basic_spec(1, execution=ExecutionPolicy(max_retries=5)))
    assert res.runs[0].attempts == 1 and a.calls[res.runs[0].run_id] == 1 and res.runs[0].status == RunStatus.FAILED


async def test_execute_is_idempotent_no_duplicate_adapter_calls_or_measurements():
    a = ScriptedAdapter("a")
    s = svc(a)
    spec = basic_spec(3)
    plan = s.design(spec)
    r1 = await s.execute(plan, spec)
    r2 = await s.execute(plan, spec)
    r3 = await s.run(spec)
    assert all(v == 1 for v in a.calls.values()) and len(a.calls) == 3
    assert [len(r.measurements) for r in r3.runs] == [1, 1, 1] and len(r3.runs) == 3
    assert [r.run_id for r in r1.runs] == [r.run_id for r in r2.runs]


async def test_adapter_validation_rejection_fails_without_retry():
    a = ScriptedAdapter("a", validate_error=True)
    res = await svc(a).run(basic_spec(1, execution=ExecutionPolicy(max_retries=3)))
    r = res.runs[0]
    assert r.status == RunStatus.FAILED and "rejected by adapter" in r.error and r.attempts == 1 and not a.calls


async def test_required_metrics_and_invalid_data_fail_runs_instead_of_completing():
    spec = basic_spec(1).model_copy(
        update={
            "measurement_definitions": [MeasurementDefinition(metric="y", unit="u"), MeasurementDefinition(metric="z", unit="u")]
        }
    )
    res = await svc(ScriptedAdapter("a")).run(spec)
    assert res.runs[0].status == RunStatus.FAILED and "missing required metrics: ['z']" in res.runs[0].error
    assert res.runs[0].measurements and res.runs[0].metadata["quality"]["missing_metrics"] == ["z"]  # data kept for inspection
    lenient = await svc(ScriptedAdapter("a"), strict_required_metrics=False).run(
        spec.model_copy(update={"experiment_id": "lenient"})
    )
    assert lenient.runs[0].status == RunStatus.COMPLETED and lenient.runs[0].metadata["quality_warnings"]
    spec2 = basic_spec(1, experiment_id="wrong-unit").model_copy(
        update={"measurement_definitions": [MeasurementDefinition(metric="y", unit="kW")]}
    )
    res2 = await svc(ScriptedAdapter("a")).run(spec2)  # adapter reports unit 'u' -> incompatible
    assert res2.runs[0].status == RunStatus.FAILED and "no valid measurements" in res2.runs[0].error


async def test_output_constraint_evaluation_recorded_per_run():
    from trialforge import Constraint

    spec = basic_spec(3, constraints=[Constraint(metric="y", maximum=10.5)])
    res = await svc(ScriptedAdapter("a")).run(spec)
    feas = {r.configuration["x"]: r.metadata["feasible"] for r in res.runs}
    assert feas == {0: True, 1: False, 2: False}
    assert res.runs[0].metadata["output_constraints"][0]["status"] == "satisfied"


# ---------------------------------------------------------------------------- cancellation
async def test_cancel_before_execution_cancels_queued_runs_and_is_idempotent():
    a = ScriptedAdapter("a")
    s = svc(a)
    spec = basic_spec(3)
    s.design(spec)
    res = await s.cancel(spec.experiment_id)
    assert res.status == ExperimentStatus.CANCELLED and {r.status for r in res.runs} == {RunStatus.CANCELLED}
    again = await s.cancel(spec.experiment_id)
    assert again.status == ExperimentStatus.CANCELLED and not a.calls
    res2 = await s.run_pending(spec.experiment_id)
    assert {r.status for r in res2.runs} == {RunStatus.CANCELLED}


async def test_cancel_in_flight_run_stops_remaining_and_keeps_completed():
    a = ScriptedAdapter("a", delay=0.15, caps=AdapterCapabilities(concurrent=False, cancellation=True))
    s = svc(a)
    spec = basic_spec(4)
    plan = s.design(spec)
    task = asyncio.create_task(s.execute(plan, spec))
    await asyncio.sleep(0.2)  # run 0 done, run 1 in flight
    await s.cancel(spec.experiment_id)
    res = await task
    st = [r.status for r in sorted(res.runs, key=lambda r: r.order)]
    assert st[0] == RunStatus.COMPLETED and set(st[1:]) == {RunStatus.CANCELLED}
    assert a.cancelled and res.status == ExperimentStatus.CANCELLED
    completed = [r for r in res.runs if r.status == RunStatus.COMPLETED]
    assert completed and all(r.measurements and r.processed_measurements for r in completed)
    assert all(not r.measurements for r in res.runs if r.status == RunStatus.CANCELLED)  # cancelled results are discarded


async def test_cancel_single_run_and_warning_when_adapter_cannot_cancel():
    a = ScriptedAdapter("a", caps=AdapterCapabilities(cancellation=False))
    s = svc(a)
    spec = basic_spec(2)
    plan = s.design(spec)
    victim = plan.runs[0].run_id
    await s.cancel(spec.experiment_id, victim)
    res = await s.execute(plan, spec)
    st = {r.run_id: r.status for r in res.runs}
    assert st[victim] == RunStatus.CANCELLED and list(st.values()).count(RunStatus.COMPLETED) == 1


# ------------------------------------------------------------------------ delayed measurements
async def test_delayed_measurements_wait_then_complete_via_collect_pending():
    a = ScriptedAdapter(
        "a", delayed=True, caps=AdapterCapabilities(delayed_measurements=True, immediate_execution=False, resumable=True)
    )
    s = svc(a)
    spec = basic_spec(2)
    res = await s.run(spec)
    assert res.status == ExperimentStatus.WAITING_FOR_MEASUREMENTS
    assert {r.status for r in res.runs} == {RunStatus.WAITING_FOR_MEASUREMENTS} and all(
        r.adapter_handle.startswith("h-") for r in res.runs
    )
    res = await s.collect_pending(spec.experiment_id)
    assert res.status == ExperimentStatus.WAITING_FOR_MEASUREMENTS and not any(r.measurements for r in res.runs)
    a.ready.add(res.runs[0].run_id)
    res = await s.collect_pending(spec.experiment_id)
    done = [r for r in res.runs if r.status == RunStatus.COMPLETED]
    assert len(done) == 1 and done[0].measurements[0].value == 42.0 and res.status == ExperimentStatus.WAITING_FOR_MEASUREMENTS
    a.ready.add(res.runs[1].run_id)
    assert (await s.collect_pending(spec.experiment_id)).status == ExperimentStatus.COMPLETED


# -------------------------------------------------------------------------------- concurrency
@pytest.mark.parametrize(
    "policy,adapter_max,concurrent,expected", [(5, 3, True, 3), (2, 8, True, 2), (5, 8, False, 1), (1, 8, True, 1)]
)
async def test_concurrency_is_limited_by_policy_and_adapter_capabilities(policy, adapter_max, concurrent, expected):
    a = ScriptedAdapter("a", delay=0.03, caps=AdapterCapabilities(concurrent=concurrent, max_concurrency=adapter_max))
    s = svc(a)
    res = await s.run(basic_spec(8, execution=ExecutionPolicy(max_concurrency=policy)))
    assert res.status == ExperimentStatus.COMPLETED and a.max_in_flight == expected


async def test_adapters_without_execute_run_are_serialised_and_measurements_stay_with_their_run():
    a = LegacyAdapter()
    s = svc(a)
    res = await s.run(basic_spec(6, execution=ExecutionPolicy(max_concurrency=6)))
    assert a.max_in_flight == 1 and res.status == ExperimentStatus.COMPLETED
    for r in res.runs:
        assert r.measurements[0].run_id == r.run_id and r.measurements[0].value == 10 + r.configuration["x"]
        assert r.measurements[0].metadata["adapter_run_id"] == "unused" and "run_id_bound" in r.measurements[0].quality_flags


async def test_run_timeout_fails_run_and_asks_adapter_to_cancel():
    a = ScriptedAdapter("a", delay=1.0)
    res = await svc(a).run(basic_spec(1, execution=ExecutionPolicy(run_timeout_seconds=0.05)))
    assert res.runs[0].status == RunStatus.FAILED and "timed out" in res.runs[0].error and a.cancelled == [res.runs[0].run_id]


async def test_budget_is_enforced_at_execution_time():
    s = svc(ScriptedAdapter("a"))
    spec = basic_spec(3, run_budget=3)
    plan = s.design(spec)
    s.repository.save_experiment(spec)
    extra = plan.runs[0].model_copy(update={"run_id": "extra", "order": 9})
    s.repository.save_run(extra)
    with pytest.raises(ExecutionError, match="exceeding its budget"):
        await s.run_pending(spec.experiment_id)


# ---------------------------------------------------------------------------- background runner
async def test_local_background_runner_executes_waits_and_cancels():
    a = ScriptedAdapter("a", delay=0.02)
    s = svc(a)
    runner = LocalBackgroundRunner(s)
    spec = basic_spec(3)
    s.design(spec)
    t1 = runner.submit(spec.experiment_id)
    assert runner.submit(spec.experiment_id) is t1 and runner.is_running(spec.experiment_id)
    assert (await runner.wait(spec.experiment_id)).status == ExperimentStatus.COMPLETED
    slow = ScriptedAdapter("s", delay=0.2, caps=AdapterCapabilities(cancellation=True))
    s2 = svc(slow)
    r2 = LocalBackgroundRunner(s2)
    spec2 = basic_spec(5)
    s2.design(spec2)
    r2.submit(spec2.experiment_id)
    await asyncio.sleep(0.05)
    res = await r2.cancel(spec2.experiment_id)
    assert res.status == ExperimentStatus.CANCELLED
    await r2.shutdown()
