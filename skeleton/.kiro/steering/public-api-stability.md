# Steering: public API stability
- The public API is `trialforge.__all__` (top-level imports). `tests/public_api_snapshot.txt` lists the names that must never disappear; additions are fine.
- Deep module paths used by 0.1 (`trialforge.models.core`, `trialforge.designer.engine`, ...) keep working.
- Behaviour changes need a note in the changelog/report. Prefer new optional fields with defaults over changes to existing ones; deprecate before removing.
- Bump `trialforge.__version__` (and `pyproject.toml`) with every release; `API_VERSION` changes only on breaking changes.
- Report schema: `ExperimentReport.report_version` bumps minor for added optional fields, major for removals/renames.
