# Execution, retries and restart recovery

## Adapter selection and capabilities

`ExperimentSpec.adapter_name` names the adapter in the `AdapterRegistry`. With exactly one registered adapter the name is optional; with
several, executing an experiment without a name raises `AdapterUnavailableError` (designing without executing is allowed). Capability
checks at validation: a spec with `time_series=True` metrics requires an adapter declaring `time_series`.

| Capability | Effect |
|---|---|
| `concurrent`, `max_concurrency` | effective concurrency = `min(policy.max_concurrency, adapter.max_concurrency)`, or 1 if not concurrent |
| `retryable` | retries (`ExecutionPolicy.max_retries`) only happen if True |
| `idempotent` | restart recovery may re-queue an interrupted run automatically |
| `resumable` | restart recovery may query the adapter (`status`, `collect_result`) instead of re-executing |
| `cancellation` | `cancel()` forwards to the adapter; otherwise a warning is stored on the run |
| `delayed_measurements`, `immediate_execution` | execution may return a handle and complete later via `collect_pending()` |

Adapters that implement `execute_run(context)` are called concurrently. Adapters with only `prepare()` + `execute()` share state between
the two calls, so the engine **serialises** them per adapter (and binds the placeholder run ids `""`, `"unused"`, `"unbound"` to the
right run).

## Retries and idempotency

`ExecutionPolicy(max_retries, retry_delay_seconds, run_timeout_seconds)`. Validation rejections by the adapter are never retried.
A failed run stays `FAILED`; re-running the experiment does **not** retry it. Use `service.retry_run(experiment_id, run_id)` explicitly.
`execute()` / `run_pending()` are idempotent: finished runs are never re-executed. Each run carries `idempotency_key =
"<experiment_id>:<run_id>"` (passed to adapters in `ExecutionContext`). Retries re-run the same run and do not consume budget.

## Restart recovery

A run is persisted as `RUNNING` before the adapter is called. If the process dies it stays `RUNNING`. After restart:

```python
service = ExperimentService(registry=registry, repository=SQLiteExperimentRepository("sqlite:///experiments.db"))
actions = await service.recover()  # decides per run according to ExecutionPolicy.recovery and capabilities
result = await service.run_pending(experiment_id)
```

| `ExecutionPolicy.recovery` | Behaviour |
|---|---|
| `auto` (default) | resumable → resume; else idempotent → re-queue; else **fail** (never silently retried or completed) |
| `resume` | query the adapter; fail if not resumable |
| `retry` | re-queue if retryable |
| `fail` | mark interrupted runs FAILED |

Runs `WAITING_FOR_MEASUREMENTS` after a restart are recovered only for resumable adapters; otherwise they are failed with an
explanation. A completed run is never duplicated or altered. The durable job table (`jobs`) records pending work; `recover()` returns
claimed jobs to pending.

## Cancellation

`cancel(experiment_id)` cancels all non-terminal runs; `cancel(experiment_id, run_id)` one run. In-flight results of a cancelled run are
discarded. Cancelling is idempotent.

## Background execution

`LocalBackgroundRunner(service).submit(experiment_id)` runs `run_pending` as an asyncio task in the current loop (no Redis/Celery).
State is durable because the service persists it, not because of the runner.
