# trialforge-core

The TRIALFORGE core SDK: experiment design, execution, measurement processing, statistical analysis, optimisation,
persistence and reporting. Domain-independent; applications supply adapters.

```bash
pip install -e .            # from this directory; Python 3.11+
pip install -e ".[dev]"     # tests, reference implementations (statsmodels), ruff
```

```python
from trialforge import ExperimentSpec, ExperimentService, ExperimentDesigner, Analyzer, Optimizer
```

Documentation, examples and the architecture description are in the repository root:
[`README.md`](../../README.md), [`docs/`](../../docs/) and [`examples/`](../../examples/).

Run the tests from the repository root: `python -m pytest` (the example adapters live outside this package).
