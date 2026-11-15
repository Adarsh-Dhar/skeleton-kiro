# Installation

Requirements: Python 3.11 or newer. Runtime dependencies: `pydantic`, `SQLAlchemy`, `numpy`, `scipy`.

```bash
git clone <repository> trialforge && cd trialforge
python -m venv .venv && . .venv/bin/activate
pip install -e ./packages/trialforge-core
python -c "import trialforge; print(trialforge.__version__)"
```

| Extra | Adds | Needed for |
|---|---|---|
| `optuna` | Optuna | `Optimizer.suggest(..., method="optuna")` |
| `test` | pytest, pytest-asyncio, Optuna | running the test-suite |
| `verify` | statsmodels, pandas | independent cross-check tests |
| `dev` | all of the above + ruff | development |

Optuna's `GPSampler` additionally needs PyTorch; without it the `optuna` strategy transparently uses Optuna's TPE sampler and
labels the recommendation accordingly (`strategy="optuna:TPESampler ..."`). The built-in `bayesian` strategy has no extra dependency.

## Verify the checkout

```bash
pip install -e "./packages/trialforge-core[dev]"
python -m pytest                     # run from the repository root
python scripts/check_imports.py      # every public name and example imports
./scripts/lint.sh
python -m examples.quickstart        # run from the repository root
```

The example adapters live in the top-level `examples/` directory (not in the installable package), so run examples and tests from the
repository root. Your own application only needs `pip install trialforge-core` and its own adapter module.

## Durable storage

`ExperimentService()` uses an in-memory SQLite database (nothing is written to disk). For restart-safe operation pass a file:

```python
from trialforge import ExperimentService, SQLiteExperimentRepository

service = ExperimentService(adapter, repository=SQLiteExperimentRepository("sqlite:///experiments.db"))
```

Any SQLAlchemy URL works (for example PostgreSQL), but only SQLite is exercised by this project's tests.
