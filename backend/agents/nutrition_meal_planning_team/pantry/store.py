"""Postgres-backed pantry store (SPEC-015 §4.3).

One row per ``(client_id, canonical_id)``. Re-adding an existing
canonical id sums ``quantity_grams`` instead of inserting a duplicate,
using ``INSERT ... ON CONFLICT`` so the semantics are race-free.
"""

from __future__ import annotations

import functools
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, List, Literal, Optional

from shared_postgres import dict_row, get_conn
from shared_postgres.metrics import timed_query

from .errors import InvalidQuantity, PantryItemNotFound
from .types import PantryItem

logger = logging.getLogger(__name__)

_STORE = "nutrition_meal_planning"

# Sentinel used by ``update_item`` to distinguish "field omitted" (keep
# existing value) from "set to NULL" (explicit clear). Callers that want
# to clear a nullable column pass ``None``; default-argument behaviour
# (the sentinel) leaves the column untouched.
_UNSET: Any = object()

# The full column list returned to callers, shared by every SELECT and
# RETURNING in this module so column additions are one-line changes.
_ITEM_COLUMNS = (
    "client_id, canonical_id, quantity_grams, display_qty, display_unit, "
    "expires_on, notes, added_at, updated_at"
)
_SELECT_ITEM = f"SELECT {_ITEM_COLUMNS} FROM nutrition_pantry"

SortMode = Literal["expiring", "name", "added_desc"]

_SORT_CLAUSES: dict[SortMode, str] = {
    # Items with the soonest expiry first; items with no expiry date sink
    # to the bottom. Tiebreak alphabetically so the order is stable.
    "expiring": "expires_on ASC NULLS LAST, canonical_id ASC",
    "name": "canonical_id ASC",
    "added_desc": "added_at DESC, canonical_id ASC",
}


def _row_ts(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _row_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _row_to_item(row: dict) -> PantryItem:
    return PantryItem(
        client_id=row["client_id"],
        canonical_id=row["canonical_id"],
        quantity_grams=float(row["quantity_grams"]),
        display_qty=(float(row["display_qty"]) if row.get("display_qty") is not None else None),
        display_unit=row.get("display_unit"),
        expires_on=_row_date(row.get("expires_on")),
        notes=row.get("notes") or "",
        added_at=_row_ts(row.get("added_at")),
        updated_at=_row_ts(row.get("updated_at")),
    )


@timed_query(store=_STORE, op="pantry.add_or_increment_item")
def add_or_increment_item(
    client_id: str,
    canonical_id: str,
    quantity_grams: float,
    *,
    display_qty: Optional[float] = None,
    display_unit: Optional[str] = None,
    expires_on: Optional[date] = None,
    notes: Optional[str] = None,
) -> PantryItem:
    """Insert a pantry row or increment the existing one for the same canonical id.

    On conflict, ``quantity_grams`` is summed; display metadata is
    updated only when the caller passes a non-null value (``COALESCE``
    keeps the prior value otherwise).
    """
    if quantity_grams <= 0:
        raise InvalidQuantity(f"quantity_grams must be positive, got {quantity_grams!r}")

    sql = (
        "INSERT INTO nutrition_pantry "
        "(client_id, canonical_id, quantity_grams, display_qty, display_unit, "
        " expires_on, notes) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (client_id, canonical_id) DO UPDATE SET "
        "  quantity_grams = nutrition_pantry.quantity_grams + EXCLUDED.quantity_grams, "
        "  display_qty    = COALESCE(EXCLUDED.display_qty,  nutrition_pantry.display_qty), "
        "  display_unit   = COALESCE(EXCLUDED.display_unit, nutrition_pantry.display_unit), "
        "  expires_on     = COALESCE(EXCLUDED.expires_on,   nutrition_pantry.expires_on), "
        "  notes          = COALESCE(EXCLUDED.notes,        nutrition_pantry.notes), "
        "  updated_at     = now() "
        f"RETURNING {_ITEM_COLUMNS}"
    )
    params = (client_id, canonical_id, quantity_grams, display_qty, display_unit, expires_on, notes)
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return _row_to_item(row)


@timed_query(store=_STORE, op="pantry.update_item")
def update_item(
    client_id: str,
    canonical_id: str,
    *,
    quantity_grams: Optional[float] = None,
    display_qty: Any = _UNSET,
    display_unit: Any = _UNSET,
    expires_on: Any = _UNSET,
    notes: Any = _UNSET,
) -> PantryItem:
    """Update selected columns on an existing pantry row.

    Raises :class:`PantryItemNotFound` if the row does not exist.
    Unlike :func:`add_or_increment_item`, this replaces ``quantity_grams``
    outright (setting, not incrementing).

    For the four nullable columns (``display_qty``, ``display_unit``,
    ``expires_on``, ``notes``) this function uses a sentinel-based API:
    omitting the argument leaves the column untouched, while passing
    ``None`` explicitly clears it to SQL ``NULL``. ``quantity_grams`` is
    ``NOT NULL`` in the schema, so ``None`` there means "don't update".
    """
    if quantity_grams is not None and quantity_grams <= 0:
        raise InvalidQuantity(f"quantity_grams must be positive, got {quantity_grams!r}")

    # Build the SET clause dynamically so unspecified fields are not touched.
    # For nullable columns, only the sentinel ``_UNSET`` means "skip"; an
    # explicit ``None`` is a request to clear the column.
    fields: list[str] = []
    params: list[Any] = []
    if quantity_grams is not None:
        fields.append("quantity_grams = %s")
        params.append(quantity_grams)
    if display_qty is not _UNSET:
        fields.append("display_qty = %s")
        params.append(display_qty)
    if display_unit is not _UNSET:
        fields.append("display_unit = %s")
        params.append(display_unit)
    if expires_on is not _UNSET:
        fields.append("expires_on = %s")
        params.append(expires_on)
    if notes is not _UNSET:
        fields.append("notes = %s")
        params.append(notes)
    if not fields:
        # Nothing to update; just return the current row.
        item = get_item(client_id, canonical_id)
        if item is None:
            raise PantryItemNotFound(f"{client_id}/{canonical_id}")
        return item

    fields.append("updated_at = now()")
    params.extend([client_id, canonical_id])

    sql = (
        f"UPDATE nutrition_pantry SET {', '.join(fields)} "
        "WHERE client_id = %s AND canonical_id = %s "
        f"RETURNING {_ITEM_COLUMNS}"
    )
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, tuple(params))
        row = cur.fetchone()
    if row is None:
        raise PantryItemNotFound(f"{client_id}/{canonical_id}")
    return _row_to_item(row)


@timed_query(store=_STORE, op="pantry.delete_item")
def delete_item(client_id: str, canonical_id: str) -> bool:
    """Remove a pantry row. Returns True if a row was deleted."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM nutrition_pantry WHERE client_id = %s AND canonical_id = %s",
            (client_id, canonical_id),
        )
        return cur.rowcount > 0


@timed_query(store=_STORE, op="pantry.get_item")
def get_item(client_id: str, canonical_id: str) -> Optional[PantryItem]:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"{_SELECT_ITEM} WHERE client_id = %s AND canonical_id = %s",
            (client_id, canonical_id),
        )
        row = cur.fetchone()
    return _row_to_item(row) if row else None


@timed_query(store=_STORE, op="pantry.list_items")
def list_items(client_id: str, *, sort: SortMode = "expiring") -> List[PantryItem]:
    order_by = _SORT_CLAUSES.get(sort, _SORT_CLAUSES["expiring"])
    sql = f"{_SELECT_ITEM} WHERE client_id = %s ORDER BY {order_by}"
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (client_id,))
        return [_row_to_item(r) for r in cur.fetchall()]


@timed_query(store=_STORE, op="pantry.list_expiring")
def list_expiring(client_id: str, days: int = 3) -> List[PantryItem]:
    """Return items whose ``expires_on`` is within ``days`` of today.

    Items already past their expiry date are included — they are the most
    urgent hint for the planner (SPEC-015 §4.7).
    """
    if days < 0:
        raise ValueError("days must be non-negative")
    cutoff = datetime.now(tz=timezone.utc).date() + timedelta(days=days)
    sql = (
        f"{_SELECT_ITEM} WHERE client_id = %s AND expires_on IS NOT NULL "
        "  AND expires_on <= %s "
        "ORDER BY expires_on ASC, canonical_id ASC"
    )
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (client_id, cutoff))
        return [_row_to_item(r) for r in cur.fetchall()]


class PantryStore:
    """Thin wrapper around the module-level CRUD functions.

    Stateless; the connection pool lives inside ``shared_postgres``.
    Matches the shape of other nutrition team stores so it can be
    dependency-injected into agents and FastAPI handlers.
    """

    def add_or_increment_item(self, *args: Any, **kwargs: Any) -> PantryItem:
        return add_or_increment_item(*args, **kwargs)

    def update_item(self, *args: Any, **kwargs: Any) -> PantryItem:
        return update_item(*args, **kwargs)

    def delete_item(self, client_id: str, canonical_id: str) -> bool:
        return delete_item(client_id, canonical_id)

    def get_item(self, client_id: str, canonical_id: str) -> Optional[PantryItem]:
        return get_item(client_id, canonical_id)

    def list_items(self, client_id: str, *, sort: SortMode = "expiring") -> List[PantryItem]:
        return list_items(client_id, sort=sort)

    def list_expiring(self, client_id: str, days: int = 3) -> List[PantryItem]:
        return list_expiring(client_id, days)


@functools.cache
def get_pantry_store() -> PantryStore:
    """Return the process-wide pantry store, instantiating on first call."""
    return PantryStore()
