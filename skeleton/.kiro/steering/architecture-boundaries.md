# Steering: architecture boundaries
- Layers depend downward only (`tests/test_architecture.py::LAYERS` is the definition). A new cross-layer import needs a deliberate edit of that table in the same change.
- `models/core.py` and `adapters/contracts.py` are the **only** definitions of shared types. Never redefine or shadow them elsewhere.
- Analysis, optimisation and reporting must not import `execution` or `persistence`. Use `trialforge.evaluation` for outcome/feasibility/Pareto logic; never reimplement it.
- The core never imports `examples` or anything domain-specific, and never mentions a domain by name in code or strings.
- No `eval`, `exec`, `compile`, `__import__`, `importlib.import_module`, `pickle`, `subprocess` in `src/` (tested). Extensions are registered callables, referenced by name.
