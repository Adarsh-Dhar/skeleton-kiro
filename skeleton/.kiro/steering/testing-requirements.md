# Steering: testing requirements
- Run the whole suite before declaring anything done: `python -m pytest` from the repository root; report real pass/fail/skip counts.
- Layers: `unit/`, `integration/`, `contracts/` (one suite for **all** adapters), `regression/` (one test per fixed defect), root `tests/` (architecture, docs).
- Tests of concurrency/serialisation must fail when the mechanism is removed (mutation-check them).
- Crash/restart behaviour is tested against a file database with a simulated process death.
- Any new adapter gets a case in `contracts/test_adapter_contract.py`. Any new public API gets docs; docs snippets marked `# docs-test` are executed.
- Never hide a failing test; never skip without a reason that names the missing dependency.
