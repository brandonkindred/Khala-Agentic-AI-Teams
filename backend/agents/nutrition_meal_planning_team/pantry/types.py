"""Pantry domain types (SPEC-015 §4.2).

Only the types needed by Phase 0 (the store) are defined here.
``PantryImportDraft`` / ``ProposedItem`` land with W6 (bulk-import parser).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class PantryItem:
    """One pantry row, keyed by (client_id, canonical_id).

    ``quantity_grams`` is the canonical storage unit; ``display_qty`` and
    ``display_unit`` preserve what the user entered so the UI can render
    "2 onions" rather than "200 g".
    """

    client_id: str
    canonical_id: str
    quantity_grams: float
    display_qty: Optional[float] = None
    display_unit: Optional[str] = None
    expires_on: Optional[date] = None
    notes: str = ""
    added_at: str = ""
    updated_at: str = ""
