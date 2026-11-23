"""F1 SETUP LAB: an application built on the TRIALFORGE SDK.

All F1 knowledge lives here; all experiment machinery comes from ``trialforge``. The app works with two data modes that are
kept separate and labelled everywhere:

* ``synthetic``: setup experiments on an invented vehicle model (``f1lab.adapters``). No car or timing data is involved.
* ``historical``: real Formula 1 sessions imported from OpenF1 (https://openf1.org, ``f1lab.integrations.historical``) for
  analysis and benchmarking only. They contain no car setup and never become setup-experiment results.

The application is not affiliated with Formula 1, OpenF1, or any team.
"""

__version__ = "2.1.0"

SYNTHETIC_NOTICE = (
    "SYNTHETIC DATA: the vehicle model is an invented response surface. This is not real Formula 1 telemetry, "
    "and synthetic experiments use no car or timing data."
)
HISTORICAL_NOTICE = (
    "HISTORICAL F1 DATA: real Formula 1 session data from OpenF1 (unofficial, not affiliated with Formula 1). "
    "Used for analysis only; some values are estimated from it, and the import coverage on each session says which."
)
NOTICES = {"synthetic": SYNTHETIC_NOTICE, "historical": HISTORICAL_NOTICE}


def notice_for(mode: str) -> str:
    """The data-source notice for a supported mode. An unknown mode is an error, never silently labelled synthetic."""
    try:
        return NOTICES[mode]
    except KeyError:
        raise ValueError(f"unknown data mode {mode!r}; supported: {', '.join(NOTICES)}") from None
