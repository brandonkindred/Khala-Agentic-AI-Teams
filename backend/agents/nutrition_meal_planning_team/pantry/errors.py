"""Pantry error types (SPEC-015)."""

from __future__ import annotations


class PantryError(Exception):
    """Base class for pantry errors."""


class PantryItemNotFound(PantryError):
    """Raised when update/delete targets a (client_id, canonical_id) that does not exist."""


class InvalidQuantity(PantryError):
    """Raised when a non-positive quantity_grams is supplied on add/update."""
