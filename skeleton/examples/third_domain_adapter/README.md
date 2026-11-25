# Third-domain example: coffee roasting (synthetic)

A complete TRIALFORGE experiment in a domain the SDK knows nothing about. **Every measurement is synthetic.** The roaster is an
invented, deterministic response surface in `adapter.py`. It is not real coffee-roasting data and is not a model of any real roaster.

Run it from `skeleton/`, or from the repository root with `scripts/demo-coffee-integration.sh`:

    python -m examples.third_domain_adapter.run_example

`run_example.py` does this, in order:

1. Registers the adapter explicitly: `registry = AdapterRegistry(); registry.register(CoffeeRoastAdapter())`.
2. Builds an `ExperimentSpec` with `adapter_name="coffee-roast"`. The service looks that name up in the registry.
3. Designs the experiment, executes every run through the adapter, analyses the result, asks the optimiser for the next
   configurations, and builds a report.

The SDK contains no coffee, F1 or WATTWISE code. `tests/test_coffee_integration.py` checks this against the installed `trialforge`
package.

## Writing your own adapter

An adapter is any object that satisfies the `ExperimentAdapter` protocol, importing only from the public `trialforge` package.

| Member | Purpose |
| --- | --- |
| `metadata: AdapterMetadata` | Unique `name`, `version`, `domain`, and `synthetic=True/False`. Set `synthetic=True` unless values come from a real system. |
| `capabilities: AdapterCapabilities` | Declare `immediate_execution` or `delayed_measurements`, `concurrent`, `max_concurrency`, `retryable`, `idempotent`. |
| `async validate(config)` | Raise `AdapterError` for a configuration the system cannot run. |
| `async prepare(context)` | Called before a run with an `ExecutionContext` (run id, configuration, metadata). |
| `async execute_run(context)` / `execute(config)` | Run the configuration and return `ExecutionResponse(completed=True, measurements=[Measurement(...)])`. |
| `async collect_result(run_id)` | For delayed adapters: return the measurements once they exist. Immediate adapters raise `AdapterError`. |
| `async status(handle)`, `async cancel(run_id)` | Report progress and stop a run. |

Steps:

1. Copy `adapter.py`, rename the class, and set `metadata.name` to your domain name.
2. Replace `roast_model` and `_measure` with calls to your system (or your own simulator). Return `Measurement` objects with a
   `metric`, `value`, `unit` and `source`.
3. Register it: `registry.register(MyAdapter())`.
4. Set `adapter_name="<your metadata.name>"` in your `ExperimentSpec`. With exactly one registered adapter the name is optional.
5. Use `ExperimentService(registry=registry)` for `design`, `execute`, `analyse`, `optimise` and `report`.

Real measurements should be labelled with a `source` that names the real system, and `metadata.synthetic` should be `False`.
