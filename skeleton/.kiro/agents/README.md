# Kiro agents: ownership map

Parallel agents are used **only** for work with disjoint file ownership. Shared contracts have exactly one owner.

| Agent | Owns (may edit) | Must not edit |
|---|---|---|
| `contracts-owner` | `models/core.py`, `adapters/contracts.py`, `errors/`, `__init__.py`, `tests/public_api_snapshot.txt` | everything else |
| `statistics-owner` | `analysis/`, `evaluation/`, `tests/unit/test_statistics.py` | contracts |
| `execution-owner` | `execution/`, `persistence/`, `history/`, `tests/integration/` | contracts, analysis |
| `designer-measurements-owner` | `designer/`, `measurements/`, matching unit tests | contracts |
| `optimisation-owner` | `optimization/`, `tests/unit/test_optimization.py` | contracts |
| `examples-docs-owner` | `examples/`, `docs/`, `README.md`, `tests/test_docs.py`, `tests/contracts/` | `src/` |

JSON definitions are provided for the three contract-critical owners (`contracts-owner`, `statistics-owner`, `execution-owner`); the other rows
are ownership rules for human or agent work. In this delivery all work was done by a single agent session; the files document the
intended boundaries and were not exercised in parallel.

Rule: an agent that needs a change to a shared type asks `contracts-owner`; it never redefines the type locally.
Verification gates after every wave: `scripts/lint.sh`, `python -m pytest`, `python scripts/check_imports.py`.
