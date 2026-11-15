# Steering: statistical correctness
- The run is the unit of analysis; never treat raw samples as independent replicates.
- Never pool different treatments. Every estimate lists the runs used and the runs excluded, with reasons.
- Never report an interval or p-value the data cannot support: use `evidence="insufficient"` with an explanation.
- Use t-based inference (Welch, paired); no 1.96 shortcuts. State assumptions and warn on violations; do not silently "fix" data.
- Never claim causality from association. Fractional designs: report aliasing.
- Infeasible (constraint-violating or unknown) runs never become "best" or part of the Pareto front.
- A heuristic is never presented as a calibrated prediction (`Recommendation.prediction_kind`).
- New statistics need an **independent** reference test (scipy/statsmodels/closed form/simulation), not a re-statement of the implementation.
