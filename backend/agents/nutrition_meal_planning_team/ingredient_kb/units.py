"""Unit registry + conversion helpers.

SPEC-005 §4.8. Loads ``units.yaml`` once, exposes conversion helpers
that return ``None`` on missing density rather than silently falling
back to a fake number.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from .catalog import get_densities
from .errors import KBError, UnknownUnitError
from .types import Unit, UnitKind

_DATA_DIR = Path(__file__).resolve().parent / "data"


def _load_units_yaml() -> dict:
    path = _DATA_DIR / "units.yaml"
    if not path.exists():
        raise FileNotFoundError(f"ingredient_kb data missing: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise KBError("units.yaml root must be a mapping")
    return data


@lru_cache(maxsize=None)
def get_units() -> dict[str, Unit]:
    out: dict[str, Unit] = {}
    for name, row in _load_units_yaml().items():
        try:
            kind = UnitKind(row["kind"])
        except (KeyError, ValueError) as exc:
            raise KBError(f"units.yaml: unit {name!r} missing or bad 'kind'") from exc
        out[name] = Unit(
            name=name,
            kind=kind,
            grams_per_unit=row.get("grams_per_unit"),
            ml_per_unit=row.get("ml_per_unit"),
        )
    return out


def get_unit(name: str) -> Unit:
    """Resolve a unit name, raising UnknownUnitError if unknown."""
    units = get_units()
    if name not in units:
        raise UnknownUnitError(f"unknown unit: {name!r}")
    return units[name]


def is_known_unit(name: str) -> bool:
    return name in get_units()


def convert_to_grams(*, qty: float, unit: Unit, canonical_id: Optional[str]) -> Optional[float]:
    """Convert ``qty`` of ``unit`` to grams. Returns None on missing density.

    - Mass units: direct multiplication by ``grams_per_unit``.
    - Volume units: needs a per-canonical-id density entry in
      ``densities.yaml``. Missing density → None (caller treats as a
      structured issue, not a silent zero).
    - Count units: needs a per-canonical-id mass entry; same failure
      mode.
    """
    if qty is None or unit is None:
        return None
    if unit.kind == UnitKind.mass:
        if unit.grams_per_unit is None:
            return None
        return qty * unit.grams_per_unit
    if canonical_id is None:
        return None
    densities = get_densities().get(canonical_id) or {}
    if unit.kind == UnitKind.volume:
        if unit.ml_per_unit is None:
            return None
        volume_to_mass = densities.get("volume_to_mass")
        if volume_to_mass is None:
            return None
        return qty * unit.ml_per_unit * float(volume_to_mass)
    if unit.kind == UnitKind.count:
        count_to_mass = densities.get("count_to_mass")
        if count_to_mass is None:
            return None
        # `dozen` expands to 12.
        multiplier = qty * (12 if unit.name == "dozen" else 1)
        return multiplier * float(count_to_mass)
    return None


def default_qty_grams(canonical_id: str) -> Optional[float]:
    """Return the qty-less fallback mass ("a handful of cashews") or None.

    SPEC-005 §7 documents we ship ~20 of these for the most common
    qty-less patterns; anything else falls through to a missing_qty
    issue.
    """
    densities = get_densities().get(canonical_id) or {}
    value = densities.get("default_qty_grams")
    return float(value) if value is not None else None
