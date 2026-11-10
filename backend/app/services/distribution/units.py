"""Explicit unit conversion rules for comparing and filtering attribute values.

Only units listed here are converted. Values in different dimensions (e.g. "W" vs "V") or with unknown
units are never compared; callers report them as "not comparable" instead of guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.services.normalization.normalize import KNOWN_UNITS

# dimension -> (base unit, {unit: factor to base})
DIMENSIONS: dict[str, tuple[str, dict[str, Decimal]]] = {
    "data": ("GB", {"MB": Decimal("0.001"), "GB": Decimal(1), "TB": Decimal(1000)}),
    "mass": ("g", {"mg": Decimal("0.001"), "g": Decimal(1), "kg": Decimal(1000), "lb": Decimal("453.59237"),
                   "oz": Decimal("28.349523125")}),
    "length": ("mm", {"mm": Decimal(1), "cm": Decimal(10), "m": Decimal(1000), "in": Decimal("25.4"),
                      "ft": Decimal("304.8")}),
    "voltage": ("V", {"mV": Decimal("0.001"), "V": Decimal(1), "kV": Decimal(1000)}),
    "voltage_ac": ("VAC", {"VAC": Decimal(1)}),
    "voltage_dc": ("VDC", {"VDC": Decimal(1)}),
    "current": ("A", {"mA": Decimal("0.001"), "A": Decimal(1)}),
    # 1 mechanical horsepower = 745.69987158227022 W (explicit rule; metric hp is not assumed)
    "power": ("W", {"W": Decimal(1), "kW": Decimal(1000), "HP": Decimal("745.69987158227022")}),
    "volume": ("mL", {"mL": Decimal(1), "L": Decimal(1000)}),
    "charge": ("mAh", {"mAh": Decimal(1), "Ah": Decimal(1000)}),
    "frequency": ("Hz", {"Hz": Decimal(1), "kHz": Decimal(1000)}),
    "pressure": ("kPa", {"kPa": Decimal(1), "bar": Decimal(100), "psi": Decimal("6.894757293168")}),
    "speed": ("RPM", {"RPM": Decimal(1)}),
    "torque": ("Nm", {"Nm": Decimal(1)}),
    "temperature": ("°C", {"°C": Decimal(1)}),
    "resolution": ("MP", {"MP": Decimal(1)}),
}
UNIT_DIMENSION = {u: d for d, (_, units) in DIMENSIONS.items() for u in units}
EXTRA_UNITS = {"mv": "mV", "kv": "kV", "lbs": "lb", "grams": "g", "gram": "g", "kilograms": "kg", "kilogram": "kg",
               "meter": "m", "meters": "m", "metre": "m", "metres": "m", "centimeters": "cm", "millimeters": "mm",
               "watts": "W", "watt": "W", "volts": "V", "volt": "V", "amps": "A", "amp": "A"}
NUM_UNIT = re.compile(r"^\s*(?P<num>[-+]?\d+(?:[.,]\d+)?)\s*(?P<unit>[A-Za-zµΩ°%\"']{0,12})\s*$")


@dataclass(frozen=True)
class Quantity:
    value: Decimal  # in the dimension's base unit
    dimension: str
    unit: str  # base unit
    original: str


def canonical_unit(unit: str | None) -> str | None:
    if not unit:
        return None
    u = unit.strip()
    if u in UNIT_DIMENSION:
        return u
    low = u.lower()
    return KNOWN_UNITS.get(low) or EXTRA_UNITS.get(low) or (u if u in UNIT_DIMENSION else None)


def parse_quantity(value: str | None, unit: str | None = None) -> Quantity | None:
    """'512', 'GB' -> 512 GB; '1 TB' -> 1000 GB. Returns None when the value is not a plain number + known unit."""
    if value is None:
        return None
    text = str(value).strip()
    m = NUM_UNIT.match(text)
    if not m:
        return None
    u = canonical_unit(unit or m.group("unit") or None)
    if u is None or u not in UNIT_DIMENSION:
        return None
    try:
        num = Decimal(m.group("num").replace(",", "."))
    except InvalidOperation:
        return None
    dim = UNIT_DIMENSION[u]
    base, factors = DIMENSIONS[dim]
    return Quantity(num * factors[u], dim, base, f"{text} {unit}".strip() if unit and unit not in text else text)


def parse_number(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation:
        return None


def display(q: Quantity) -> str:
    v = q.value.normalize()
    s = format(v, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return f"{s} {q.unit}"


def normalized(value: str | None, unit: str | None) -> dict | None:
    q = parse_quantity(value, unit)
    if q is None:
        return None
    return {"value": float(q.value), "unit": q.unit, "dimension": q.dimension}
