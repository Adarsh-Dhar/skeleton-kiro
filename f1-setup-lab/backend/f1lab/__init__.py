"""F1 SETUP LAB: a viewer and analysis workstation for real historical Formula 1 sessions from OpenF1 (https://openf1.org).

Sessions are imported from OpenF1's free historical API (2023 onwards, no account) into the root database and analysed in the
browser: laps, telemetry, track location, running order, results, gaps, pit stops, race control, team radio and weather.
The application is not affiliated with Formula 1 or OpenF1.
"""

__version__ = "2.0.0"

HISTORICAL_NOTICE = (
    "HISTORICAL F1 DATA: real Formula 1 session data from OpenF1 (unofficial, not affiliated with Formula 1). "
    "Used for analysis only; some values are estimated from it, and the import coverage on each session says which."
)
