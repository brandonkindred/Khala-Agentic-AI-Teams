"""Postgres-backed store for clients and brands with versioning.

Data is persisted in the shared Khala Postgres instance via
``shared.postgres.get_conn``. DDL lives in ``branding_team.postgres`` and
is registered from the team's FastAPI lifespan.

Every public method is wrapped in ``@timed_query`` so slow reads and
writes surface as structured log lines.

Note for maintainers:
    ``tests/test_store.py`` exercises this module's SQL against live Postgres
    via ``shared.postgres.testing.real_postgres_schema`` (skips when
    ``POSTGRES_HOST`` is unset). Conversation and session SQL live in
    ``tests/test_conversation_store.py`` and ``tests/test_session_store.py``.
    When you change or add SQL here, update those suites — there is no
    parallel in-memory SQL emulator to keep in sync.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from psycopg import Cursor
from psycopg.types.json import Json

from shared.postgres import PostgresHelperMixin
from shared.postgres.metrics import timed_query
from user_profile import ArtifactType, record_association_safe, remove_association_safe

from .models import (
    Brand,
    BrandingMission,
    BrandPhase,
    BrandStatus,
    BrandVersionSummary,
    Client,
    TeamOutput,
)

logger = logging.getLogger(__name__)

_STORE = "branding"


class BrandVersionAppendConflict(RuntimeError):
    """Raised when a brand-version append cannot persist because the brand row is gone.

    Subclasses ``RuntimeError`` so broad ``except Exception`` / job-failure paths
    still catch it, while the sync ``POST /run`` handler can single it out for
    HTTP 409 without mapping unrelated runtime failures (e.g. LLM/provider errors)
    to a client conflict.
    """


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _validate_pagination(limit: Optional[int], offset: int) -> None:
    """Enforce pagination preconditions with real raises (survives ``python -O``).

    Preconditions:
        ``limit`` is None or a positive int; ``offset`` is >= 0.
    """
    if limit is not None and limit <= 0:
        raise ValueError("limit must be None or a positive int")
    if offset < 0:
        raise ValueError("offset must be >= 0")


class AttachConversationResult(str, Enum):
    """Outcome of :meth:`BrandingStore.attach_conversation`."""

    OK = "ok"
    CONVERSATION_NOT_FOUND = "conversation_not_found"
    ALREADY_ATTACHED = "already_attached"
    BRAND_NOT_FOUND = "brand_not_found"


class _AttachAbort(Exception):
    """Internal control-flow signal: abort the attach transaction with *result*."""

    def __init__(self, result: AttachConversationResult) -> None:
        self.result = result


def _apply_brand_patch(cur: Cursor, brand_id: str, client_id: str, patch: dict) -> Optional[Brand]:
    """Shallow-merge *patch* into a brand's JSONB and return the updated Brand.

    The single server-side ``data || patch ... RETURNING data`` write that
    both ``update_brand`` and ``append_brand_version`` share. Runs on the
    caller's cursor (so it participates in the caller's transaction); returns
    None when no row matched (e.g. a concurrent delete).
    """
    # ``%s::jsonb`` cast is required: psycopg adapts ``Json`` as the ``json``
    # type, and Postgres has no ``jsonb || json`` operator (both operands of
    # ``||`` must be jsonb).
    cur.execute(
        "UPDATE branding_brands SET data = data || %s::jsonb "
        "WHERE id = %s AND client_id = %s RETURNING data",
        (Json(patch), brand_id, client_id),
    )
    row = cur.fetchone()
    return Brand.model_validate(row["data"]) if row is not None else None


class BrandingStore(PostgresHelperMixin):
    """Postgres-backed store for clients and brands.

    The constructor takes no arguments — the Postgres DSN is read from
    the ``POSTGRES_*`` env vars by ``shared.postgres.get_conn``. The
    store itself is stateless; the pool is owned by shared.postgres.
    """

    def __init__(self) -> None:
        # Stateless; the connection pool lives inside shared.postgres.
        super().__init__()

    # ------------------------------------------------------------------
    # Clients
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="get_client")
    def get_client(self, client_id: str) -> Optional[Client]:
        """Retrieve a client by id.

        Preconditions:
            ``client_id`` is a non-empty string.
        Postconditions:
            Returns the validated ``Client`` when a row exists, else ``None``.
        """
        if not client_id:
            raise ValueError("client_id must be a non-empty string")
        row = self._fetch_one("SELECT data FROM branding_clients WHERE id = %s", (client_id,))
        if row is None:
            return None
        return Client.model_validate(row["data"])

    @timed_query(store=_STORE, op="list_clients")
    def list_clients(self, limit: Optional[int] = None, offset: int = 0) -> List[Client]:
        """Return clients, optionally paginated.

        Preconditions:
            ``limit`` is None or a positive int; ``offset`` is >= 0.
        Postconditions:
            Rows are ordered by ``(created_at, id)`` — the ``id`` tie-breaker
            keeps pagination stable when rows share a ``created_at``. When
            ``limit`` is None the full set is returned; otherwise at most
            ``limit`` rows starting at ``offset``.
        """
        _validate_pagination(limit, offset)
        if limit is None:
            rows = self._fetch_all("SELECT data FROM branding_clients ORDER BY created_at, id")
        else:
            rows = self._fetch_all(
                "SELECT data FROM branding_clients ORDER BY created_at, id LIMIT %s OFFSET %s",
                (limit, offset),
            )
        return [Client.model_validate(r["data"]) for r in rows]

    @timed_query(store=_STORE, op="create_client")
    def create_client(
        self,
        name: str,
        contact_info: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Client:
        """Insert a new client row and return the created model.

        Preconditions:
            ``name`` is a non-empty string; ``contact_info`` / ``notes`` are
            optional free-form strings when provided.
        Postconditions:
            Returns a ``Client`` whose ``id`` is freshly minted
            (``client_<hex>``) and whose timestamps are set. A matching row
            exists in ``branding_clients``.
        """
        if not name:
            raise ValueError("name must be a non-empty string")
        client_id = f"client_{uuid4().hex[:12]}"
        now = _now_iso()
        client = Client(
            id=client_id,
            name=name,
            created_at=now,
            updated_at=now,
            contact_info=contact_info,
            notes=notes,
        )
        self._execute(
            "INSERT INTO branding_clients (id, data) VALUES (%s, %s)",
            (client_id, Json(client.model_dump(mode="json"))),
        )
        return client

    # ------------------------------------------------------------------
    # Brands
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="get_brand")
    def get_brand(self, client_id: str, brand_id: str) -> Optional[Brand]:
        """Retrieve a brand scoped to its owning client.

        Preconditions:
            ``client_id`` and ``brand_id`` are non-empty strings.
        Postconditions:
            Returns the validated ``Brand`` when a row exists for that
            ``(id, client_id)`` pair, else ``None`` (including when the brand
            exists under a different client).
        """
        if not client_id:
            raise ValueError("client_id must be a non-empty string")
        if not brand_id:
            raise ValueError("brand_id must be a non-empty string")
        row = self._fetch_one(
            "SELECT data FROM branding_brands WHERE id = %s AND client_id = %s",
            (brand_id, client_id),
        )
        if row is None:
            return None
        return Brand.model_validate(row["data"])

    @timed_query(store=_STORE, op="brand_exists")
    def brand_exists(self, brand_id: str) -> bool:
        """True if a brand with *brand_id* exists for any client.

        Single indexed lookup — replaces scanning every client's brand list.

        Postconditions:
            Returns a bool; performs exactly one query and loads no JSONB.
        """
        row = self._fetch_one("SELECT 1 FROM branding_brands WHERE id = %s LIMIT 1", (brand_id,))
        return row is not None

    @timed_query(store=_STORE, op="get_brand_by_id")
    def get_brand_by_id(self, brand_id: str) -> Optional[Tuple[str, Brand]]:
        """Return ``(client_id, Brand)`` for *brand_id* regardless of client.

        Single query — replaces the O(clients) scan callers used when they
        hold a brand id but not its owning client.

        Invariants:
            ``branding_brands.id`` is the table's ``PRIMARY KEY`` (globally
            unique across clients), so ``WHERE id = %s`` matches at most one row
            — no ``LIMIT 1`` needed and the resolved client is unambiguous.
        Postconditions:
            Returns None when no such brand exists, else the owning client id
            paired with the validated Brand.
        """
        row = self._fetch_one(
            "SELECT client_id, data FROM branding_brands WHERE id = %s",
            (brand_id,),
        )
        if row is None:
            return None
        return row["client_id"], Brand.model_validate(row["data"])

    @timed_query(store=_STORE, op="get_brand_names")
    def get_brand_names(self, brand_ids: List[str]) -> Dict[str, Optional[str]]:
        """Return a ``{brand_id: name}`` map for the requested ids only.

        Preconditions:
            ``brand_ids`` is a list of brand id strings.
        Postconditions:
            The result contains an entry for every requested id that exists;
            unknown ids are simply absent. A value may be ``None`` when the
            stored JSONB document has no ``name`` key. Empty input yields an
            empty map with no query issued.
        """
        unique_ids = list({bid for bid in brand_ids if bid})
        if not unique_ids:
            return {}
        rows = self._fetch_all(
            "SELECT id, data FROM branding_brands WHERE id = ANY(%s)",
            (unique_ids,),
        )
        # Read the name straight out of the JSONB document — no need to build
        # and validate a full Brand model just to pull one field.
        return {r["id"]: r["data"].get("name") for r in rows}

    @timed_query(store=_STORE, op="list_brands_for_client")
    def list_brands_for_client(
        self, client_id: str, limit: Optional[int] = None, offset: int = 0
    ) -> List[Brand]:
        """Return a client's brands, optionally paginated.

        Preconditions:
            ``limit`` is None or a positive int; ``offset`` is >= 0.
        Postconditions:
            Rows are ordered by ``(created_at, id)`` — the ``id`` tie-breaker
            keeps pagination stable when rows share a ``created_at``. When
            ``limit`` is None all of the client's brands are returned;
            otherwise at most ``limit`` rows starting at ``offset``.
        """
        _validate_pagination(limit, offset)
        if limit is None:
            rows = self._fetch_all(
                "SELECT data FROM branding_brands WHERE client_id = %s ORDER BY created_at, id",
                (client_id,),
            )
        else:
            rows = self._fetch_all(
                "SELECT data FROM branding_brands WHERE client_id = %s "
                "ORDER BY created_at, id LIMIT %s OFFSET %s",
                (client_id, limit, offset),
            )
        return [Brand.model_validate(r["data"]) for r in rows]

    @timed_query(store=_STORE, op="create_brand")
    def create_brand(
        self,
        client_id: str,
        mission: BrandingMission,
        name: Optional[str] = None,
    ) -> Optional[Brand]:
        """Insert a draft brand for an existing client.

        Preconditions:
            ``client_id`` is a non-empty string; ``mission`` is a validated
            ``BrandingMission``; ``name`` is optional (defaults to
            ``mission.company_name``).
        Postconditions:
            Returns ``None`` when no client row exists for ``client_id``
            (no brand row is written). Otherwise returns a ``Brand`` with a
            freshly minted ``brand_<hex>`` id, ``status=draft``,
            ``version=0``, empty history, and a matching
            ``branding_brands`` row. Best-effort profile association is
            attempted after commit and never raises.
        """
        if not client_id:
            raise ValueError("client_id must be a non-empty string")
        if not isinstance(mission, BrandingMission):
            raise ValueError("mission must be a BrandingMission")
        with self._transaction() as cur:
            cur.execute("SELECT 1 FROM branding_clients WHERE id = %s", (client_id,))
            if cur.fetchone() is None:
                return None
            brand_id = f"brand_{uuid4().hex[:12]}"
            now = _now_iso()
            brand = Brand(
                id=brand_id,
                client_id=client_id,
                name=name or mission.company_name,
                status=BrandStatus.draft,
                mission=mission,
                latest_output=None,
                version=0,
                history=[],
                created_at=now,
                updated_at=now,
            )
            cur.execute(
                "INSERT INTO branding_brands (id, client_id, data) VALUES (%s, %s, %s)",
                (brand_id, client_id, Json(brand.model_dump(mode="json"))),
            )
        # Best-effort: link the brand to the default profile. record_association_safe
        # never raises, so a link failure can't break brand creation.
        record_association_safe(ArtifactType.BRAND, "branding", brand_id, label=brand.name)
        return brand

    @timed_query(store=_STORE, op="delete_brand")
    def delete_brand(self, client_id: str, brand_id: str) -> bool:
        """Delete a brand row and its best-effort profile association.

        Postconditions:
            Returns True iff a brand row was deleted; False (not an error)
            when no such row exists for this client — the expected outcome
            when a concurrent request already deleted it. Callers use this to
            roll back a brand ``create_brand`` committed moments earlier once
            a subsequent conversation attach fails.
        """
        deleted = self._execute(
            "DELETE FROM branding_brands WHERE id = %s AND client_id = %s",
            (brand_id, client_id),
        )
        if deleted > 0:
            remove_association_safe(ArtifactType.BRAND, brand_id)
        return deleted > 0

    @timed_query(store=_STORE, op="update_brand")
    def update_brand(
        self,
        client_id: str,
        brand_id: str,
        mission: Optional[BrandingMission] = None,
        status: Optional[BrandStatus] = None,
        name: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> Optional[Brand]:
        """Patch a brand's mutable fields in a single round trip.

        Uses a server-side ``jsonb`` shallow merge (``data || patch``) so only
        the changed keys travel to Postgres — the full brand document (mission
        + every phase output + history) is never read back and re-serialised
        just to flip ``status`` or attach a ``conversation_id``.

        Preconditions:
            ``client_id`` and ``brand_id`` are non-empty strings; when
            provided, ``mission`` is a ``BrandingMission`` and ``status`` is a
            ``BrandStatus``. ``name`` and ``conversation_id`` are optional
            free-form strings.
        Postconditions:
            Returns the updated Brand, or None when no such brand exists for
            the given client.

            When ``mission`` is provided, the update invalidates any
            previously generated output: it clears the brand's
            ``latest_output`` and resets ``current_phase`` back to
            ``BrandPhase.STRATEGIC_CORE.value`` so downstream consumers
            recompute against the new mission instead of serving stale
            positioning.
        """
        if not client_id:
            raise ValueError("client_id must be a non-empty string")
        if not brand_id:
            raise ValueError("brand_id must be a non-empty string")
        if mission is not None and not isinstance(mission, BrandingMission):
            raise ValueError("mission must be a BrandingMission")
        if status is not None and not isinstance(status, BrandStatus):
            raise ValueError("status must be a BrandStatus")
        patch: dict = {"updated_at": _now_iso()}
        if mission is not None:
            patch["mission"] = mission.model_dump(mode="json")
            # A mission edit invalidates any previously generated output: it was
            # produced from the *old* mission, so ``latest_output`` (and the
            # phase progress it represents) no longer reflect this brand. Clear
            # them so downstream consumers — notably the design-assets endpoint,
            # which reuses ``latest_output.strategic_core`` — recompute from the
            # current mission instead of serving stale positioning.
            patch["latest_output"] = None
            patch["current_phase"] = BrandPhase.STRATEGIC_CORE.value
        if status is not None:
            patch["status"] = status.value
        if name is not None:
            patch["name"] = name
        if conversation_id is not None:
            patch["conversation_id"] = conversation_id
        with self._transaction() as cur:
            return _apply_brand_patch(cur, brand_id, client_id, patch)

    @timed_query(store=_STORE, op="attach_conversation")
    def attach_conversation(
        self, client_id: str, brand_id: str, conversation_id: str, mission: BrandingMission
    ) -> Tuple[AttachConversationResult, Optional[Brand]]:
        """Attach an existing conversation to *brand_id* and patch the brand, atomically.

        Locks the conversation row (``FOR UPDATE``) before checking whether it is
        already attached elsewhere, then updates both the conversation and the
        brand in the same transaction. This closes two races a check-then-write
        sequence across separate transactions would leave open: another request
        attaching the same conversation between the uniqueness check and the
        write, and the brand row disappearing after the conversation was already
        attached (which would otherwise leave the conversation pointing at a
        brand that never learns its id).

        Preconditions:
            ``client_id``, ``brand_id``, ``conversation_id`` are non-empty
            strings; ``mission`` is a valid :class:`BrandingMission`.
        Postconditions:
            On :attr:`AttachConversationResult.OK`, the conversation row now has
            ``brand_id`` set to *brand_id* and ``mission_json`` set to *mission*,
            the brand's ``conversation_id`` is set to *conversation_id*, and the
            updated :class:`Brand` is returned. Any other result leaves both
            rows unchanged (the transaction rolls back) and the paired value is
            ``None``.
        """
        if not client_id:
            raise ValueError("client_id must be a non-empty string")
        if not brand_id:
            raise ValueError("brand_id must be a non-empty string")
        if not conversation_id:
            raise ValueError("conversation_id must be a non-empty string")
        if not isinstance(mission, BrandingMission):
            raise ValueError("mission must be a BrandingMission")
        try:
            with self._transaction() as cur:
                cur.execute(
                    "SELECT brand_id FROM branding_conversations WHERE conversation_id = %s FOR UPDATE",
                    (conversation_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise _AttachAbort(AttachConversationResult.CONVERSATION_NOT_FOUND)
                current_brand_id = row["brand_id"]
                if current_brand_id and str(current_brand_id) != brand_id:
                    raise _AttachAbort(AttachConversationResult.ALREADY_ATTACHED)

                ts = datetime.now(tz=timezone.utc)
                cur.execute(
                    "UPDATE branding_conversations SET brand_id = %s, mission_json = %s, updated_at = %s "
                    "WHERE conversation_id = %s",
                    (brand_id, Json(mission.model_dump(mode="json")), ts, conversation_id),
                )

                patch = {"conversation_id": conversation_id, "updated_at": _now_iso()}
                brand = _apply_brand_patch(cur, brand_id, client_id, patch)
                if brand is None:
                    raise _AttachAbort(AttachConversationResult.BRAND_NOT_FOUND)
        except _AttachAbort as exc:
            return exc.result, None
        return AttachConversationResult.OK, brand

    @timed_query(store=_STORE, op="append_brand_version")
    def append_brand_version(
        self,
        client_id: str,
        brand_id: str,
        output: TeamOutput,
    ) -> Optional[Brand]:
        """Append a new version, writing only the changed keys.

        The new version number and history list are derived from the current
        record, but the write is a ``jsonb`` shallow merge so untouched
        top-level keys (e.g. ``mission``) are not re-serialised and re-sent.

        Postconditions:
            On success the brand's ``version`` increments by one, the output
            is recorded as ``latest_output``, and a history entry is appended.
            Returns None if the brand no longer exists at write time (e.g. a
            concurrent delete between the read and the write).
        """
        with self._transaction() as cur:
            # Read only the two fields we need to compute the next version,
            # not the whole brand document (which embeds the previous
            # latest_output — every phase's output). FOR UPDATE locks the row
            # for the transaction (get_conn commits at block exit) so
            # concurrent appends serialise and no increment is lost.
            cur.execute(
                "SELECT data->>'version' AS version, data->'history' AS history "
                "FROM branding_brands WHERE id = %s AND client_id = %s FOR UPDATE",
                (brand_id, client_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            now = _now_iso()
            new_version = int(row["version"] or 0) + 1
            history_entry = BrandVersionSummary(
                version=new_version,
                created_at=now,
                status=output.status.value,
            )
            new_history = list(row["history"] or []) + [history_entry.model_dump(mode="json")]
            patch = {
                "latest_output": output.model_dump(mode="json"),
                "current_phase": output.current_phase.value,
                "version": new_version,
                "history": new_history,
                "updated_at": now,
            }
            return _apply_brand_patch(cur, brand_id, client_id, patch)


# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------

_store_lock = threading.Lock()
_default_store: Optional[BrandingStore] = None


def get_default_store() -> BrandingStore:
    """Return the process-wide store, instantiating on first call.

    Postconditions:
        Returns the singleton ``BrandingStore``. Concurrent first calls race
        safely (double-checked locking under ``_store_lock``) — exactly one
        instance is constructed and every caller observes the same object.
    """
    global _default_store
    if _default_store is None:
        with _store_lock:
            if _default_store is None:
                _default_store = BrandingStore()
    return _default_store
