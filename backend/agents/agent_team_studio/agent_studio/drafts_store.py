"""In-memory Agent Studio drafts store (local/dev when Postgres is unset).

User-scoped persistence of handoff state + partial Stage work as an opaque
``payload`` dict. Process-lifetime only — no LRU eviction (drafts are
user-owned durable-intent data; the Postgres twin is the multi-worker path).

Thread-safe via a single ``threading.Lock`` around the record map.

Invariants:
    * Every stored record is keyed by ``draft_id`` and carries a ``user_id``.
    * Ops for the wrong ``user_id`` behave as not-found.
    * ``len`` of internal map only changes via ``create`` / ``delete``.
"""

from __future__ import annotations

import copy
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .models import AgentStudioDraft, AgentStudioDraftSummary

_LIST_DEFAULT_LIMIT = 50
_LIST_MAX_LIMIT = 100


def iso_now() -> str:
    """Return an aware UTC ISO-8601 timestamp string.

    Postconditions:
        * The string parses as an aware datetime; timezone is UTC.
    """
    return datetime.now(timezone.utc).isoformat()


def default_draft_name() -> str:
    """Timestamp label used when the caller omits ``name`` on create.

    Postconditions:
        * Returns a non-empty string.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def validate_user_id(user_id: str) -> str:
    """Reject empty / whitespace-only user ids.

    Preconditions:
        * ``user_id`` is a ``str``.
    Postconditions:
        * Returns the stripped ``user_id``.
    Raises:
        ValueError: when empty after strip.
    """
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id must be a non-empty string")
    return user_id.strip()


def validate_optional_name(name: str | None) -> str | None:
    """Validate an optional name; ``None`` means leave unchanged / use default.

    Raises:
        ValueError: when ``name`` is provided but empty/whitespace.
    """
    if name is None:
        return None
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    return name.strip()


def validate_optional_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate an optional opaque payload object.

    Raises:
        ValueError: when ``payload`` is not ``None`` and not a ``dict``.
    """
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    return payload


def clamp_pagination(limit: int, offset: int) -> tuple[int, int]:
    """Clamp list pagination to the UX-spec contract.

    Postconditions:
        * Returned ``limit`` is in ``[1, 100]`` (default intent 50 applied by callers
          before clamp when they pass the default).
        * Returned ``offset`` is ``>= 0``.
    """
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = _LIST_DEFAULT_LIMIT
    try:
        off = int(offset)
    except (TypeError, ValueError):
        off = 0
    lim = max(1, min(lim, _LIST_MAX_LIMIT))
    off = max(0, off)
    return lim, off


@dataclass
class _DraftRecord:
    draft_id: str
    user_id: str
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class AgentStudioDraftStore:
    """Process-local, user-scoped drafts store."""

    def __init__(self) -> None:
        self._records: dict[str, _DraftRecord] = {}
        self._lock = threading.Lock()

    def create(
        self,
        user_id: str,
        *,
        name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AgentStudioDraft:
        """Create a new draft owned by ``user_id``.

        Preconditions:
            * ``user_id`` non-empty; ``name`` if given non-empty; ``payload`` if given a dict.
        Postconditions:
            * Returns a new draft with a fresh ``draft_id``; ``get(user_id, id)`` resolves it.
        """
        uid = validate_user_id(user_id)
        resolved_name = validate_optional_name(name)
        if resolved_name is None:
            resolved_name = default_draft_name()
        resolved_payload = validate_optional_payload(payload)
        if resolved_payload is None:
            resolved_payload = {}
        now = iso_now()
        draft_id = str(uuid.uuid4())
        record = _DraftRecord(
            draft_id=draft_id,
            user_id=uid,
            name=resolved_name,
            payload=copy.deepcopy(resolved_payload),
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._records[draft_id] = record
        return self._to_draft(record)

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
        """
        uid = validate_user_id(user_id)
        new_name = validate_optional_name(name)
        new_payload = validate_optional_payload(payload)
        with self._lock:
            record = self._records.get(draft_id)
            if record is None or record.user_id != uid:
                return None
            if new_name is not None:
                record.name = new_name
            if new_payload is not None:
                record.payload = copy.deepcopy(new_payload)
            record.updated_at = iso_now()
            return self._to_draft(record)

    def get(self, user_id: str, draft_id: str) -> AgentStudioDraft | None:
        """Return the full draft if owned by ``user_id``, else ``None``."""
        uid = validate_user_id(user_id)
        with self._lock:
            record = self._records.get(draft_id)
            if record is None or record.user_id != uid:
                return None
            return self._to_draft(record)

    def list_summaries(
        self, user_id: str, *, limit: int = 50, offset: int = 0
    ) -> list[AgentStudioDraftSummary]:
        """List owned draft summaries, most-recent ``updated_at`` first."""
        uid = validate_user_id(user_id)
        lim, off = clamp_pagination(limit, offset)
        with self._lock:
            owned = [r for r in self._records.values() if r.user_id == uid]
            owned.sort(key=lambda r: r.updated_at, reverse=True)
            page = owned[off : off + lim]
            return [
                AgentStudioDraftSummary(
                    draft_id=r.draft_id, name=r.name, updated_at=r.updated_at
                )
                for r in page
            ]

    def rename(self, user_id: str, draft_id: str, name: str) -> AgentStudioDraftSummary | None:
        """Rename an owned draft; ``None`` if missing or wrong user."""
        uid = validate_user_id(user_id)
        new_name = validate_optional_name(name)
        assert new_name is not None  # rename requires a name
        with self._lock:
            record = self._records.get(draft_id)
            if record is None or record.user_id != uid:
                return None
            record.name = new_name
            record.updated_at = iso_now()
            return AgentStudioDraftSummary(
                draft_id=record.draft_id, name=record.name, updated_at=record.updated_at
            )

    def delete(self, user_id: str, draft_id: str) -> bool:
        """Delete an owned draft; ``False`` if missing or wrong user."""
        uid = validate_user_id(user_id)
        with self._lock:
            record = self._records.get(draft_id)
            if record is None or record.user_id != uid:
                return False
            del self._records[draft_id]
            return True

    @staticmethod
    def _to_draft(record: _DraftRecord) -> AgentStudioDraft:
        return AgentStudioDraft(
            draft_id=record.draft_id,
            name=record.name,
            created_at=record.created_at,
            updated_at=record.updated_at,
            payload=copy.deepcopy(record.payload),
        )
