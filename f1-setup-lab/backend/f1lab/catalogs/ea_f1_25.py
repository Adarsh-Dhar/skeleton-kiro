"""EA SPORTS F1 25 setup factors that can be varied in a live-simulator experiment.

``packet_field`` is the field name decoded from the Car Setups UDP packet. ``verified`` stays False until the factor has passed the min/max probe
(``scripts/probe_setup.py``): move only that control to its lowest and highest position in the garage and confirm the packet value follows the number
shown in the game. Experiments that use an unverified factor are flagged in the UI, the recommendation and the report.
Allowed values are chosen per experiment by the user (2 to 7 values from what the game offers), not hard-coded here.
"""

from __future__ import annotations

GAME_ID = "ea_f1_25"
SANITY_RANGE = (0.0, 100.0)  # generic guard only; the game's real limits come from the probe

GAME_FACTORS: dict[str, dict] = {
    "front_wing": {"label": "Front wing", "unit": "game step", "packet_field": "front_wing", "verified": False},
    "rear_wing": {"label": "Rear wing", "unit": "game step", "packet_field": "rear_wing", "verified": False},
    "on_throttle_diff": {"label": "On-throttle differential", "unit": "percent", "packet_field": "on_throttle_diff", "verified": False},
    "rear_anti_roll_bar": {"label": "Rear anti-roll bar", "unit": "game step", "packet_field": "rear_anti_roll_bar", "verified": False},
}
GAME_LABEL = {k: v["label"] for k, v in GAME_FACTORS.items()}
