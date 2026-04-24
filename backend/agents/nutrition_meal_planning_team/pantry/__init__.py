"""Pantry module for the nutrition & meal planning team (SPEC-015).

Phase 0 surface: domain types and Postgres-backed CRUD. Subtraction
(W4), API endpoints (W5), bulk import (W6), near-expiry hints (W7),
and UI (W9-W12) land in follow-up PRs per PLAN-015.
"""

from __future__ import annotations

from .errors import InvalidQuantity, PantryError, PantryItemNotFound
from .store import PantryStore, SortMode, get_pantry_store
from .types import PantryItem
from .version import PANTRY_VERSION

__all__ = [
    "PANTRY_VERSION",
    "InvalidQuantity",
    "PantryError",
    "PantryItem",
    "PantryItemNotFound",
    "PantryStore",
    "SortMode",
    "get_pantry_store",
]
