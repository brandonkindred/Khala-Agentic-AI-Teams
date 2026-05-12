"""Postgres-backed storage for meal recommendations and feedback.

Each recommendation is a row in ``nutrition_recommendations``; feedback
is inlined onto the same row (the relationship is always 1:1 and
nullable feedback columns model "no feedback yet").
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from shared_postgres import Json, dict_row, get_conn
from shared_postgres.metrics import timed_query

from ..models import FeedbackRecord, MealHistoryEntry

logger = logging.getLogger(__name__)

_STORE = "nutrition_meal_planning"


def _row_ts(value: Any) -> str:
    """Normalize a Postgres TIMESTAMPTZ to an ISO-8601 string."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 string into a timezone-aware datetime, or None on failure.

    Matches the pre-migration behavior of silently skipping unparseable
    date-range bounds instead of surfacing an error.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@timed_query(store=_STORE, op="record_recommendation")
def record_recommendation(
    client_id: str,
    meal_snapshot: Dict[str, Any],
    *,
    guardrail_version: Optional[str] = None,
    parsed_ingredients: Optional[List[Dict[str, Any]]] = None,
    flags: Optional[List[Dict[str, Any]]] = None,
    restriction_snapshot_hash: Optional[str] = None,
) -> str:
    """Store a meal recommendation and return its recommendation_id.

    Guardrail metadata kwargs are nullable for the legacy / pre-W5
    insert path; SPEC-007 W5's two-pass pipeline starts populating them
    once it lands.
    """
    recommendation_id = str(uuid4())
    ts = datetime.now(tz=timezone.utc)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nutrition_recommendations "
            "(recommendation_id, client_id, meal_snapshot, recommended_at, "
            " guardrail_version, parsed_ingredients_json, flags_json, "
            " restriction_snapshot_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                recommendation_id,
                client_id,
                Json(meal_snapshot or {}),
                ts,
                guardrail_version,
                Json(parsed_ingredients) if parsed_ingredients is not None else None,
                Json(flags) if flags is not None else None,
                restriction_snapshot_hash,
            ),
        )
    return recommendation_id


@timed_query(store=_STORE, op="record_feedback")
def record_feedback(
    recommendation_id: str,
    rating: Optional[int] = None,
    would_make_again: Optional[bool] = None,
    notes: Optional[str] = None,
) -> bool:
    """Attach feedback to an existing recommendation. Returns True if found and updated."""
    ts = datetime.now(tz=timezone.utc)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE nutrition_recommendations SET "
            "  feedback_rating = COALESCE(%s, feedback_rating), "
            "  feedback_would_make_again = COALESCE(%s, feedback_would_make_again), "
            "  feedback_notes = COALESCE(%s, feedback_notes), "
            "  feedback_submitted_at = %s "
            "WHERE recommendation_id = %s",
            (rating, would_make_again, notes, ts, recommendation_id),
        )
        return cur.rowcount > 0


@timed_query(store=_STORE, op="get_meal_history")
def get_meal_history(
    client_id: str,
    limit: int = 50,
    date_range_start: Optional[str] = None,
    date_range_end: Optional[str] = None,
) -> List[MealHistoryEntry]:
    """Return past recommendations with feedback for the client, newest first."""
    clauses = ["client_id = %s"]
    params: list[Any] = [client_id]
    start_dt = _parse_iso(date_range_start)
    if start_dt is not None:
        clauses.append("recommended_at >= %s")
        params.append(start_dt)
    end_dt = _parse_iso(date_range_end)
    if end_dt is not None:
        clauses.append("recommended_at <= %s")
        params.append(end_dt)
    params.append(limit)

    sql = (
        "SELECT recommendation_id, client_id, meal_snapshot, recommended_at, "
        "       feedback_rating, feedback_would_make_again, feedback_notes, "
        "       feedback_submitted_at "
        "FROM nutrition_recommendations "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY recommended_at DESC LIMIT %s"
    )

    entries: List[MealHistoryEntry] = []
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, tuple(params))
        for r in cur.fetchall():
            has_feedback = (
                r["feedback_rating"] is not None
                or r["feedback_would_make_again"] is not None
                or (r["feedback_notes"] is not None and r["feedback_notes"] != "")
                or r["feedback_submitted_at"] is not None
            )
            feedback_record: Optional[FeedbackRecord] = None
            if has_feedback:
                feedback_record = FeedbackRecord(
                    recommendation_id=r["recommendation_id"],
                    rating=r["feedback_rating"],
                    would_make_again=r["feedback_would_make_again"],
                    notes=r["feedback_notes"] or "",
                    submitted_at=_row_ts(r["feedback_submitted_at"])
                    if r["feedback_submitted_at"] is not None
                    else None,
                )
            entries.append(
                MealHistoryEntry(
                    recommendation_id=r["recommendation_id"],
                    client_id=r["client_id"],
                    meal_snapshot=r["meal_snapshot"] or {},
                    recommended_at=_row_ts(r["recommended_at"]),
                    feedback=feedback_record,
                )
            )
    return entries


class MealFeedbackStore:
    """Postgres-backed store for meal recommendations and feedback."""

    def __init__(self) -> None:
        # Stateless; the connection pool lives inside shared_postgres.
        pass

    def record_recommendation(
        self,
        client_id: str,
        meal_snapshot: Dict[str, Any],
        *,
        guardrail_version: Optional[str] = None,
        parsed_ingredients: Optional[List[Dict[str, Any]]] = None,
        flags: Optional[List[Dict[str, Any]]] = None,
        restriction_snapshot_hash: Optional[str] = None,
    ) -> str:
        return record_recommendation(
            client_id,
            meal_snapshot,
            guardrail_version=guardrail_version,
            parsed_ingredients=parsed_ingredients,
            flags=flags,
            restriction_snapshot_hash=restriction_snapshot_hash,
        )

    def record_feedback(
        self,
        recommendation_id: str,
        rating: Optional[int] = None,
        would_make_again: Optional[bool] = None,
        notes: Optional[str] = None,
    ) -> bool:
        return record_feedback(recommendation_id, rating, would_make_again, notes)

    def get_meal_history(
        self,
        client_id: str,
        limit: int = 50,
        date_range_start: Optional[str] = None,
        date_range_end: Optional[str] = None,
    ) -> List[MealHistoryEntry]:
        return get_meal_history(client_id, limit, date_range_start, date_range_end)


# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------

_default_store: Optional[MealFeedbackStore] = None


def get_meal_feedback_store() -> MealFeedbackStore:
    """Return the process-wide store, instantiating on first call."""
    global _default_store
    if _default_store is None:
        _default_store = MealFeedbackStore()
    return _default_store
