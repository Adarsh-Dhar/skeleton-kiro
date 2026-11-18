"""F1 SETUP LAB: an application built on the TRIALFORGE SDK.

All vehicle/telemetry knowledge lives here (``f1lab.adapters``); all experiment machinery comes from ``trialforge``.
The vehicle model is SYNTHETIC: this software has no connection to any Formula 1 team or real telemetry.
"""

__version__ = "1.0.0"
SYNTHETIC_NOTICE = (
    "SYNTHETIC DATA: the vehicle model is an invented response surface. This is not real Formula 1 telemetry, "
    "and the application has no connection to any racing team, simulator licence or timing feed."
)
SIM_RECORDED_NOTICE = (
    "SIMULATOR-RECORDED DATA: laps were driven by one person in EA SPORTS F1 25 on one track and setups were read from the game's UDP output. "
    "This is not real Formula 1 telemetry, and the findings say nothing about a real car or other drivers."
)


def notice_for(mode: str) -> str:
    return SIM_RECORDED_NOTICE if mode == "live_simulator" else SYNTHETIC_NOTICE
