"""Postgres-backed Agent Studio drafts store.

Durable, cross-worker twin of
:class:`~agent_team_studio.agent_studio.drafts_store.AgentStudioDraftStore`.
Same public surface so callers (and follow-on HTTP routes) are backend-agnostic.

DDL lives in ``agent_team_studio.agent_studio.postgres`` and is registered from the
unified API lifespan. Import this module only when Postgres is enabled.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Json

from shared.postgres import get_conn
from shared.postgres.metrics import timed_query

from .drafts_store import (
    clamp_pagination,
    default_draft_name,
    iso_now,
    validate_optional_name,
    validate_optional_payload,
    validate_user_id,
)
from .models import AgentStudioDraft, AgentStudioDraftSummary

_STORE = "agent_studio_drafts"
_TABLE = "agent_studio_drafts"


def _iso(value: Any) -> str:
    """Normalize a DB timestamptz / datetime to an ISO-8601 string."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _row_to_draft(row: dict[str, Any]) -> AgentStudioDraft:
    """Map a drafts row to :class:`AgentStudioDraft`.

    Postconditions:
        * ``payload`` is always a ``dict`` (non-object JSONB coerces to ``{}``).
        * The returned payload is a deep copy so callers cannot mutate stored state.
    """
    raw = row["payload_json"]
    payload: dict[str, Any] = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    return AgentStudioDraft(
        draft_id=row["draft_id"],
        name=row["name"],
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
        payload=payload,
    )


class PostgresAgentStudioDraftStore:
    """Postgres-backed user-scoped drafts store."""

    def __init__(self, *, now_fn: Callable[[], str] | None = None) -> None:
        """Create a Postgres drafts store.

        Preconditions:
            * ``now_fn``, when provided, returns an ISO-8601 timestamp string on each call.
        """
        self._now = now_fn or iso_now

    @timed_query(store=_STORE, op="create")
    def create(
        self,
        user_id: str,
        *,
        name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AgentStudioDraft:
        """Insert a new draft row owned by ``user_id``.

        Preconditions:
            * ``user_id`` non-empty; ``name`` if given non-empty; ``payload`` if given a dict.
        Postconditions:
            * Returns a new draft with a fresh ``draft_id``; ``get(user_id, id)`` resolves it.
        """
        uid = validate_user_id(user_id)
        resolved_name = validate_optional_name(name) or default_draft_name()
        resolved_payload = validate_optional_payload(payload)
        if resolved_payload is None:
            resolved_payload = {}
        draft_id = str(uuid4())
        now = self._now()
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO {_TABLE} "
                "(draft_id, user_id, name, payload_json, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s::timestamptz, %s::timestamptz) "
                "RETURNING draft_id, name, payload_json, created_at, updated_at",
                (draft_id, uid, resolved_name, Json(resolved_payload), now, now),
            )
            row = cur.fetchone()
        assert row is not None
        return _row_to_draft(row)

    @timed_query(store=_STORE, op="update")
    def update(
        self,
        user_id: str,
        draft_id: str,
        *,
        name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AgentStudioDraft | None:
        """Patch an owned draft; ``None`` if missing or wrong user.

        Preconditions:
            * ``user_id`` non-empty; optional ``name``/``payload`` validated when provided.
        Postconditions:
            * Returns patched draft when owned; ``updated_at`` advances.
            * Omitted ``name``/``payload`` leave those fields unchanged.
            * Returns ``None`` when missing or owned by another user.
        """
        uid = validate_user_id(user_id)
        new_name = validate_optional_name(name)
        new_payload = validate_optional_payload(payload)
        now = self._now()
        sets: list[str] = ["updated_at = %s::timestamptz"]
        params: list[Any] = [now]
        if new_name is not None:
            sets.append("name = %s")
            params.append(new_name)
        if new_payload is not None:
            sets.append("payload_json = %s")
            params.append(Json(new_payload))
        params.extend([draft_id, uid])
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"UPDATE {_TABLE} SET {', '.join(sets)} "
                "WHERE draft_id = %s AND user_id = %s "
                "RETURNING draft_id, name, payload_json, created_at, updated_at",
                params,
            )
            row = cur.fetchone()
        return _row_to_draft(row) if row else None

    @timed_query(store=_STORE, op="get")
    def get(self, user_id: str, draft_id: str) -> AgentStudioDraft | None:
        """Load a full draft if owned by ``user_id``.

        Preconditions:
            * ``user_id`` non-empty.
        Postconditions:
            * Returns the draft when ``draft_id`` exists and is owned by ``user_id``.
            * Returns ``None`` when missing or owned by another user.
        """
        uid = validate_user_id(user_id)
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT draft_id, name, payload_json, created_at, updated_at "
                f"FROM {_TABLE} WHERE draft_id = %s AND user_id = %s",
                (draft_id, uid),
            )
            row = cur.fetchone()
        return _row_to_draft(row) if row else None

    @timed_query(store=_STORE, op="list_summaries")
    def list_summaries(
        self, user_id: str, *, limit: int = 50, offset: int = 0
    ) -> list[AgentStudioDraftSummary]:
        """List owned summaries, most-recent ``updated_at`` first.

        Preconditions:
            * ``user_id`` non-empty.
        Postconditions:
            * Returns summaries for ``user_id`` only, ordered ``updated_at`` DESC.
            * Pagination clamped via ``clamp_pagination`` (limit ∈ [1,100], offset ≥ 0).
            * Empty list when the user has no drafts or offset is past the end.
        """
        uid = validate_user_id(user_id)
        lim, off = clamp_pagination(limit, offset)
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT draft_id, name, updated_at FROM {_TABLE} "
                "WHERE user_id = %s ORDER BY updated_at DESC "
                "LIMIT %s OFFSET %s",
                (uid, lim, off),
            )
            rows = cur.fetchall()
        return [
            AgentStudioDraftSummary(
                draft_id=r["draft_id"], name=r["name"], updated_at=_iso(r["updated_at"])
            )
            for r in rows
        ]

    @timed_query(store=_STORE, op="rename")
    def rename(self, user_id: str, draft_id: str, name: str) -> AgentStudioDraftSummary | None:
        """Rename an owned draft; ``None`` if missing or wrong user.

        Preconditions:
            * ``user_id`` non-empty; ``name`` non-empty.
        Postconditions:
            * Returns updated summary when owned; ``updated_at`` advances.
            * Returns ``None`` when missing or owned by another user.
        Raises:
            ValueError: when ``name`` is empty/whitespace (via ``validate_optional_name``).
        """
        uid = validate_user_id(user_id)
        new_name = validate_optional_name(name)
        assert new_name is not None
        now = self._now()
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"UPDATE {_TABLE} SET name = %s, updated_at = %s::timestamptz "
                "WHERE draft_id = %s AND user_id = %s "
                "RETURNING draft_id, name, updated_at",
                (new_name, now, draft_id, uid),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return AgentStudioDraftSummary(
            draft_id=row["draft_id"], name=row["name"], updated_at=_iso(row["updated_at"])
        )

    @timed_query(store=_STORE, op="delete")
    def delete(self, user_id: str, draft_id: str) -> bool:
        """Delete an owned draft; ``False`` if missing or wrong user.

        Preconditions:
            * ``user_id`` non-empty.
        Postconditions:
            * Returns ``True`` and removes the row when owned by ``user_id``.
            * Returns ``False`` when missing or owned by another user.
        """
        uid = validate_user_id(user_id)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {_TABLE} WHERE draft_id = %s AND user_id = %s",
                (draft_id, uid),
            )
            return cur.rowcount > 0
