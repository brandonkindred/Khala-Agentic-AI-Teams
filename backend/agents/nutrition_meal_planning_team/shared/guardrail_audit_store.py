"""Postgres-backed append-only log of SPEC-007 guardrail rejections.

One row per violation (not per recommendation): a meal flagged for two
ingredients produces two rows. Reads are W11's job — this module is
write-only for now.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from shared_postgres import Json, get_conn
from shared_postgres.metrics import timed_query

logger = logging.getLogger(__name__)

_STORE = "nutrition_meal_planning"


@timed_query(store=_STORE, op="record_rejection")
def record_rejection(
    client_id: str,
    meal_snapshot: Dict[str, Any],
    violation_reason: str,
    *,
    guardrail_version: str,
    ingredient_raw: Optional[str] = None,
    canonical_id: Optional[str] = None,
    tag: Optional[str] = None,
    detail: Optional[str] = None,
    kb_version: Optional[str] = None,
) -> int:
    """Append one rejection row. Returns the new id."""
    ts = datetime.now(tz=timezone.utc)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nutrition_guardrail_rejections "
            "(client_id, meal_snapshot, violation_reason, ingredient_raw, "
            " canonical_id, tag, detail, guardrail_version, kb_version, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                client_id,
                Json(meal_snapshot or {}),
                violation_reason,
                ingredient_raw,
                canonical_id,
                tag,
                detail,
                guardrail_version,
                kb_version,
                ts,
            ),
        )
        row = cur.fetchone()
        return int(row[0])


class GuardrailAuditStore:
    """Thin Postgres-backed store for SPEC-007 guardrail rejections."""

    def __init__(self) -> None:
        # Stateless; the connection pool lives inside shared_postgres.
        pass

    def record_rejection(
        self,
        client_id: str,
        meal_snapshot: Dict[str, Any],
        violation_reason: str,
        *,
        guardrail_version: str,
        ingredient_raw: Optional[str] = None,
        canonical_id: Optional[str] = None,
        tag: Optional[str] = None,
        detail: Optional[str] = None,
        kb_version: Optional[str] = None,
    ) -> int:
        return record_rejection(
            client_id,
            meal_snapshot,
            violation_reason,
            guardrail_version=guardrail_version,
            ingredient_raw=ingredient_raw,
            canonical_id=canonical_id,
            tag=tag,
            detail=detail,
            kb_version=kb_version,
        )


# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------

_default_store: Optional[GuardrailAuditStore] = None


def get_guardrail_audit_store() -> GuardrailAuditStore:
    """Return the process-wide store, instantiating on first call."""
    global _default_store
    if _default_store is None:
        _default_store = GuardrailAuditStore()
    return _default_store
