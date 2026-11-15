"""The SAME contract suite runs against every example adapter (and any adapter you add to ADAPTERS)."""

import asyncio

import pytest
from examples.f1_simulator_adapter.adapter import SyntheticF1Adapter
from examples.third_domain_adapter.adapter import CoffeeRoastAdapter
from examples.wattwise_meter_adapter.adapter import SyntheticWattwiseAdapter

from trialforge import (
    AdapterError,
    AdapterRegistry,
    ExecutionContext,
    ExperimentAdapter,
    ExperimentService,
    ExperimentSpec,
    Factor,
    MeasurementDefinition,
    Objective,
    RunStatus,
    check_config_against_schema,
    schema_from_spec,
)


def f(name, lo, hi):
    return Factor(name=name, factor_type="continuous", lower=lo, upper=hi)


CASES = {
    "f1": dict(
        make=SyntheticF1Adapter,
        name="f1-setup-sim",
        metrics={"lap_time": "seconds", "stability": "score", "sector_1": "seconds", "top_speed": "km/h"},
        factors=[f("front_wing", 10, 40), f("rear_wing", 10, 40), f("suspension", 3, 9)],
        objective=Objective(metric="lap_time", direction="minimize"),
        valid={"front_wing": 20.0, "rear_wing": 24.0, "suspension": 5.0},
        invalid={"front_wing": 20.0, "rear_wing": 24.0},
        out_of_range={"front_wing": 99.0, "rear_wing": 24.0, "suspension": 5.0},
    ),
    "wattwise": dict(
        make=SyntheticWattwiseAdapter,
        name="wattwise-meter",
        metrics={"energy_cost": "currency", "energy_kwh": "kWh", "peak_power_kw": "kW", "departure_charge_pct": "percent"},
        factors=[
            Factor(name="charge_start_hour", factor_type="discrete", values=[0, 4, 6, 22]),
            f("charge_power_kw", 3, 11),
            f("cooling_setpoint", 20, 28),
        ],
        objective=Objective(metric="energy_cost", direction="minimize"),
        valid={"charge_start_hour": 22, "charge_power_kw": 7.0, "cooling_setpoint": 24.0},
        invalid={"charge_start_hour": 22},
        out_of_range={"charge_start_hour": 40, "charge_power_kw": 7.0, "cooling_setpoint": 24.0},
    ),
    "coffee": dict(
        make=CoffeeRoastAdapter,
        name="coffee-roast",
        metrics={"quality": "score", "moisture": "percent", "colour": "index"},
        factors=[f("temperature", 195, 215), f("duration", 6, 14), f("airflow", 0, 100)],
        objective=Objective(metric="quality", direction="maximize"),
        valid={"temperature": 205.0, "duration": 10.0, "airflow": 40.0},
        invalid={"temperature": 205.0},
        out_of_range=None,
    ),
}


@pytest.fixture(params=CASES)
def case(request):
    c = dict(CASES[request.param])
    c["id"] = request.param
    c["adapter"] = c["make"]()
    return c


def ctx(case, run_id="run-1", **kw):
    return ExecutionContext(experiment_id="e", run_id=run_id, configuration=kw.pop("config", case["valid"]), **kw)


def test_satisfies_protocol_and_declares_metadata_and_capabilities(case):
    a = case["adapter"]
    assert isinstance(a, ExperimentAdapter)
    assert a.metadata.name == case["name"] and a.metadata.version and a.metadata.description and a.metadata.domain
    assert a.metadata.synthetic is True and "synthetic" in a.metadata.tags
    assert a.capabilities.immediate_execution and a.capabilities.concurrent and a.capabilities.max_concurrency >= 1
    assert a.capabilities.idempotent and a.capabilities.retryable
    assert a.metadata.input_schema and set(a.metadata.input_schema) >= set(case["valid"])
    reg = AdapterRegistry()
    reg.register(a)
    assert case["name"] in reg and reg.get(case["name"]) is a


async def test_validate_accepts_valid_and_rejects_invalid_configuration(case):
    a = case["adapter"]
    await a.validate(case["valid"])
    with pytest.raises(AdapterError):
        await a.validate(case["invalid"])
    if case["out_of_range"]:
        with pytest.raises(AdapterError):
            await a.validate(case["out_of_range"])
    assert (
        check_config_against_schema(case["valid"], {k: v for k, v in a.metadata.input_schema.items() if k in case["valid"]}) == []
    )
    assert await a.available() is True


async def test_execute_run_returns_correct_standard_measurements(case):
    a = case["adapter"]
    resp = await a.execute_run(ctx(case, metadata={"requested_metrics": list(case["metrics"])}))
    assert resp.completed and resp.measurements
    got = {m.metric: m for m in resp.measurements}
    assert set(got) == set(case["metrics"])
    for metric, unit in case["metrics"].items():
        m = got[metric]
        assert m.unit == unit and m.run_id == "run-1" and isinstance(m.value, float) and m.source
        assert m.metadata["synthetic"] is True  # simulated data is always labelled
    assert resp.metadata["synthetic"] is True


async def test_requested_metrics_filter_and_unfiltered_default(case):
    a = case["adapter"]
    first = next(iter(case["metrics"]))
    only = await a.execute_run(ctx(case, metadata={"requested_metrics": [first]}))
    assert {m.metric for m in only.measurements} == {first}
    everything = await a.execute_run(ctx(case))
    assert set(case["metrics"]) <= {m.metric for m in everything.measurements}


async def test_deterministic_idempotent_and_replicates_differ(case):
    a = case["adapter"]
    v1 = [m.value for m in (await a.execute_run(ctx(case))).measurements]
    v2 = [m.value for m in (await a.execute_run(ctx(case))).measurements]
    v3 = [m.value for m in (await CASES[case["id"]]["make"]().execute_run(ctx(case))).measurements]
    assert v1 == v2 == v3  # same run id + config => identical (idempotent, cross-instance)
    other = [m.value for m in (await a.execute_run(ctx(case, run_id="run-2"))).measurements]
    assert other != v1  # a replicate (new run id) has independent noise
    # noise is small relative to the signal: replicates agree to a few percent
    assert all(abs(x - y) <= 0.2 * max(abs(x), 1.0) for x, y in zip(v1, other))


async def test_concurrent_execution_matches_sequential_execution(case):
    a = case["adapter"]
    ids = [f"r{i}" for i in range(6)]
    seq = {i: [m.value for m in (await a.execute_run(ctx(case, run_id=i))).measurements] for i in ids}
    par = await asyncio.gather(*(a.execute_run(ctx(case, run_id=i)) for i in ids))
    assert {i: [m.value for m in r.measurements] for i, r in zip(ids, par)} == seq


async def test_legacy_prepare_execute_path_and_unsupported_operations(case):
    a = case["adapter"]
    await a.prepare(ctx(case, run_id="legacy-1"))
    resp = await a.execute(case["valid"])
    assert resp.completed and {m.run_id for m in resp.measurements} == {"legacy-1"}
    with pytest.raises(AdapterError):
        await a.collect_result("legacy-1")  # immediate adapters have nothing to collect
    assert await a.cancel("legacy-1") is None
    assert (await a.status("h")).state == "unknown"


async def test_full_experiment_through_the_service_with_registry(case):
    reg = AdapterRegistry()
    reg.register(case["adapter"])
    spec = ExperimentSpec(
        name=f"contract-{case['id']}",
        factors=case["factors"],
        objectives=[case["objective"]],
        measurement_definitions=[MeasurementDefinition(metric=m, unit=u) for m, u in case["metrics"].items()],
        adapter_name=case["name"],
        design_method="fractional_factorial" if len(case["factors"]) >= 3 and case["id"] != "wattwise" else "factorial",
        design_options={"continuous_levels": 2, "replicates": 2},
        run_budget=40,
        random_seed=2,
    )
    s = ExperimentService(registry=reg)
    res = await s.run(spec)
    assert res.status.value == "completed" and all(r.status == RunStatus.COMPLETED for r in res.runs)
    for r in res.runs:
        assert r.measurements and all(m.run_id == r.run_id for m in r.measurements)
        assert all(not set(m.quality_flags) - {"run_id_bound"} for m in r.measurements), r.measurements[0].quality_flags
        assert {p.metric for p in r.processed_measurements} == set(case["metrics"])
    an = s.analyse(res)
    assert an.best_run_id and not an.warnings
    rep = s.report(res, an)
    assert any("SYNTHETIC" in l for l in rep.limitations) and rep.reproducibility["adapters"][0]["synthetic"] is True


def test_schema_from_spec_round_trips(case):
    spec = ExperimentSpec(
        name="x",
        factors=case["factors"],
        objectives=[case["objective"]],
        measurement_definitions=[MeasurementDefinition(metric=m, unit=u) for m, u in case["metrics"].items()],
        run_budget=5,
    )
    schema = schema_from_spec(spec)
    assert check_config_against_schema(case["valid"], schema) == []
    assert any("unexpected" in p for p in check_config_against_schema({**case["valid"], "bogus": 1}, schema))


# ----- domain-specific physics sanity (each adapter behaves like its stated model) -----------------
async def test_domain_models_respond_in_the_documented_direction():
    from examples.f1_simulator_adapter.adapter import vehicle_model
    from examples.third_domain_adapter.adapter import roast_model
    from examples.wattwise_meter_adapter.adapter import MeterSchedule, Tariff, simulate_day, summarise_readings

    base = vehicle_model({"front_wing": 24, "rear_wing": 28, "suspension": 6})
    assert base["lap_time"] == pytest.approx(base["sector_1"] + base["sector_2"] + base["sector_3"])
    assert (
        vehicle_model({"front_wing": 24, "rear_wing": 40, "suspension": 6})["top_speed"] < base["top_speed"]
    )  # more wing -> slower top speed
    assert (
        vehicle_model({"front_wing": 24, "rear_wing": 28, "suspension": 6})["stability"]
        > vehicle_model({"front_wing": 24, "rear_wing": 10, "suspension": 6})["stability"]
    )
    hot, mild = roast_model(212, 12, 40), roast_model(198, 8, 40)
    assert hot["colour"] < mild["colour"] and hot["moisture"] < mild["moisture"]
    sch = MeterSchedule()
    cfg = {"charge_start_hour": 0, "charge_power_kw": 7.0, "cooling_setpoint": 24.0}
    night = summarise_readings(simulate_day(cfg, sch)["readings"], sch.tariff)
    peak = summarise_readings(simulate_day({**cfg, "charge_start_hour": 18}, sch)["readings"], sch.tariff)
    assert night["energy_kwh"] == pytest.approx(peak["energy_kwh"], rel=0.02)  # same energy delivered
    assert night["energy_cost"] < peak["energy_cost"]  # but cheaper off-peak
    late = simulate_day({**cfg, "charge_start_hour": 6}, sch)["departure_soc_pct"]
    assert late < simulate_day(cfg, sch)["departure_soc_pct"] and late < 80  # starting too late fails the departure target
    assert Tariff().price_at(23) == 0.12 and Tariff().price_at(18) == 0.32 and Tariff().price_at(12) == 0.20
