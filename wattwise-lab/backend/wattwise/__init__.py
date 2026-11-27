"""WATTWISE LAB: household electricity experiments on the TRIALFORGE SDK.

All electricity knowledge (tariffs, EV charging, appliances, smart-meter import) lives here; all experiment machinery comes from
``trialforge``. Data policy: zero fabricated input data. Every input is the household's own (bill, meter export, appliance labels,
vehicle documents) or comes from a cited real-world source; schedule outcomes are model PROJECTIONS, never measurements.
"""

__version__ = "1.0.0"
SIMULATED_NOTICE = (
    "PROJECTED RESULTS: schedule costs, charging and load are computed by a model from the inputs you configured (your tariff, meter "
    "readings, appliances and EV). They are projections, not measurements; only meter readings you import are measured."
)
