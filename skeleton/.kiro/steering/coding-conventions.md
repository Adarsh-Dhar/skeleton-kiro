# Steering: coding conventions
- Python 3.11+, type hints on public signatures, pydantic v2 models (`extra="forbid"`).
- `ruff check` and `ruff format --check` (line length 130) must pass (`scripts/lint.sh`).
- Every public class, function and method has a docstring stating behaviour and failure modes (tested). Regenerate `docs/api_reference/index.md` with `python scripts/gen_api_reference.py`.
- Errors derive from `trialforge.errors.TrialforgeError`; keep `ValueError` compatibility for validation/design/optimisation errors.
- Persist before acting (state transitions), commit run + measurements atomically, never mutate raw measurements.
- Determinism: seed everything from `ExperimentSpec.random_seed`; never use global RNG state.
