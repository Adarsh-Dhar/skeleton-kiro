"""Validated unit registry and conversion.

Units belong to a *dimension* (time, power, energy, ...). Conversion is only
permitted within a dimension. Temperature uses affine conversion. Units that are
not in the registry (``score``, ``index``, currency codes, ...) convert only to an
identical string; they are never silently treated as compatible with anything else.
"""

from __future__ import annotations

from trialforge.errors import ValidationError

# dimension -> {canonical unit: (scale to base unit, offset in base unit)}  base = value*scale + offset
_DIMENSIONS: dict[str, dict[str, tuple[float, float]]] = {
    "time": {"s": (1.0, 0.0), "ms": (1e-3, 0.0), "us": (1e-6, 0.0), "min": (60.0, 0.0), "h": (3600.0, 0.0), "d": (86400.0, 0.0)},
    "power": {"W": (1.0, 0.0), "mW": (1e-3, 0.0), "kW": (1e3, 0.0), "MW": (1e6, 0.0)},
    "energy": {
        "J": (1.0, 0.0),
        "kJ": (1e3, 0.0),
        "MJ": (1e6, 0.0),
        "Wh": (3600.0, 0.0),
        "kWh": (3.6e6, 0.0),
        "MWh": (3.6e9, 0.0),
    },
    "temperature": {"K": (1.0, 0.0), "degC": (1.0, 273.15), "degF": (5.0 / 9.0, 459.67 * 5.0 / 9.0)},
    "length": {
        "m": (1.0, 0.0),
        "mm": (1e-3, 0.0),
        "cm": (1e-2, 0.0),
        "km": (1e3, 0.0),
        "in": (0.0254, 0.0),
        "ft": (0.3048, 0.0),
        "mi": (1609.344, 0.0),
    },
    "mass": {"kg": (1.0, 0.0), "g": (1e-3, 0.0), "mg": (1e-6, 0.0), "lb": (0.45359237, 0.0)},
    "speed": {"m/s": (1.0, 0.0), "km/h": (1 / 3.6, 0.0), "mph": (0.44704, 0.0)},
    "pressure": {"Pa": (1.0, 0.0), "kPa": (1e3, 0.0), "bar": (1e5, 0.0), "psi": (6894.757293168, 0.0)},
    "voltage": {"V": (1.0, 0.0), "mV": (1e-3, 0.0), "kV": (1e3, 0.0)},
    "current": {"A": (1.0, 0.0), "mA": (1e-3, 0.0)},
    "frequency": {"Hz": (1.0, 0.0), "kHz": (1e3, 0.0), "MHz": (1e6, 0.0)},
    "angle": {"deg": (1.0, 0.0), "rad": (57.29577951308232, 0.0)},
    "fraction": {"fraction": (1.0, 0.0), "percent": (0.01, 0.0)},
}

_ALIASES = {
    "second": "s",
    "seconds": "s",
    "sec": "s",
    "secs": "s",
    "millisecond": "ms",
    "milliseconds": "ms",
    "msec": "ms",
    "microsecond": "us",
    "microseconds": "us",
    "\u00b5s": "us",
    "minute": "min",
    "minutes": "min",
    "hour": "h",
    "hours": "h",
    "hr": "h",
    "day": "d",
    "days": "d",
    "watt": "W",
    "watts": "W",
    "kilowatt": "kW",
    "kilowatts": "kW",
    "kw": "kW",
    "milliwatt": "mW",
    "megawatt": "MW",
    "joule": "J",
    "joules": "J",
    "kilojoule": "kJ",
    "wh": "Wh",
    "watt-hour": "Wh",
    "watt_hour": "Wh",
    "kwh": "kWh",
    "kilowatt-hour": "kWh",
    "kilowatt_hour": "kWh",
    "kilowatt-hours": "kWh",
    "mwh": "MWh",
    "celsius": "degC",
    "\u00b0C": "degC",
    "degc": "degC",
    "fahrenheit": "degF",
    "\u00b0F": "degF",
    "degf": "degF",
    "kelvin": "K",
    "meter": "m",
    "meters": "m",
    "metre": "m",
    "metres": "m",
    "millimeter": "mm",
    "millimeters": "mm",
    "centimeter": "cm",
    "kilometer": "km",
    "kilometers": "km",
    "inch": "in",
    "inches": "in",
    "foot": "ft",
    "feet": "ft",
    "mile": "mi",
    "miles": "mi",
    "kilogram": "kg",
    "kilograms": "kg",
    "gram": "g",
    "grams": "g",
    "pound": "lb",
    "pounds": "lb",
    "kph": "km/h",
    "kmh": "km/h",
    "km/hr": "km/h",
    "pascal": "Pa",
    "kilopascal": "kPa",
    "kpa": "kPa",
    "volt": "V",
    "volts": "V",
    "ampere": "A",
    "amps": "A",
    "amp": "A",
    "hertz": "Hz",
    "degree": "deg",
    "degrees": "deg",
    "radian": "rad",
    "radians": "rad",
    "%": "percent",
    "pct": "percent",
    "ratio": "fraction",
}

_LOOKUP: dict[str, tuple[str, str]] = {}
for _dim, _units in _DIMENSIONS.items():
    for _u in _units:
        _LOOKUP[_u] = (_dim, _u)


class UnitRegistry:
    """Unit lookup and conversion. Extend with :meth:`register`."""

    def __init__(self) -> None:
        self.dimensions = {d: dict(u) for d, u in _DIMENSIONS.items()}
        self.aliases = dict(_ALIASES)
        self.lookup = dict(_LOOKUP)

    def register(self, dimension: str, unit: str, scale: float, offset: float = 0.0) -> None:
        """Add a unit (``base = value * scale + offset``) to a dimension."""
        if scale == 0:
            raise ValidationError("unit scale cannot be zero")
        self.dimensions.setdefault(dimension, {})[unit] = (scale, offset)
        self.lookup[unit] = (dimension, unit)

    def canonical(self, unit: str) -> tuple[str, str] | None:
        """Return ``(dimension, canonical_unit)`` or None for unknown units."""
        if unit in self.lookup:
            return self.lookup[unit]
        alias = self.aliases.get(unit) or self.aliases.get(unit.strip().lower())
        return self.lookup.get(alias) if alias else None

    def same_unit(self, a: str, b: str) -> bool:
        """True if two unit strings denote the same unit (identical or aliases)."""
        if a == b:
            return True
        ca, cb = self.canonical(a), self.canonical(b)
        return ca is not None and ca == cb

    def compatible(self, a: str, b: str) -> bool:
        """True when a value in ``a`` can be converted to ``b``."""
        if a == b:
            return True
        ca, cb = self.canonical(a), self.canonical(b)
        return ca is not None and cb is not None and ca[0] == cb[0]

    def convert(self, value: float, from_unit: str, to_unit: str) -> float:
        """Convert ``value``; raise ValidationError for incompatible/unknown units."""
        if from_unit == to_unit:
            return value
        ca, cb = self.canonical(from_unit), self.canonical(to_unit)
        if ca is None or cb is None:
            raise ValidationError(f"cannot convert '{from_unit}' to '{to_unit}': unknown unit")
        if ca[0] != cb[0]:
            raise ValidationError(f"incompatible units: '{from_unit}' ({ca[0]}) vs '{to_unit}' ({cb[0]})")
        sa, oa = self.dimensions[ca[0]][ca[1]]
        sb, ob = self.dimensions[cb[0]][cb[1]]
        base = value * sa + oa
        return (base - ob) / sb


DEFAULT_UNITS = UnitRegistry()


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """Convert using the default registry."""
    return DEFAULT_UNITS.convert(value, from_unit, to_unit)
