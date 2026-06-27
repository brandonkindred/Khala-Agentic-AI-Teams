"""Postgres-backed store for clients and brands with versioning.

Data is persisted in the shared Khala Postgres instance via
``shared_postgres.get_conn``. DDL lives in ``branding_team.postgres`` and
is registered from the team's FastAPI lifespan.

Every public method is wrapped in ``@timed_query`` so slow reads and
writes surface as structured log lines.

Note for maintainers:
    The unit tests run against an in-memory fake (``tests/_fake_postgres.py``)
    that matches the SQL emitted here by prefix. When you change or add SQL in
    this module, update that fake's handlers and the ``real_postgres``-marked
    tests in ``tests/test_store_real_postgres.py`` (which run the same SQL
    against a live Postgres in CI) so the fake can't drift into emulating
    queries the real database would reject.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from psycopg import Cursor
from psycopg.rows import dict_row
from psycopg.types.json import Json

from shared_postgres import get_conn
from shared_postgres.metrics import timed_query
from user_profile import ArtifactType, record_association_safe

from .models import (
    Brand,
    BrandingMission,
    BrandStatus,
    BrandVersionSummary,
    Client,
    TeamOutput,
)

logger = logging.getLogger(__name__)

_STORE = "branding"


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


class BrandingStore:
    """Postgres-backed store for clients and brands.

    The constructor takes no arguments — the Postgres DSN is read from
    the ``POSTGRES_*`` env vars by ``shared_postgres.get_conn``. The
    store itself is stateless; the pool is owned by shared_postgres.
    """

    def __init__(self) -> None:
        # Stateless; the connection pool lives inside shared_postgres.
        pass

    # ------------------------------------------------------------------
    # Clients
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="get_client")
    def get_client(self, client_id: str) -> Optional[Client]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT data FROM branding_clients WHERE id = %s", (client_id,))
            row = cur.fetchone()
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
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            if limit is None:
                cur.execute("SELECT data FROM branding_clients ORDER BY created_at, id")
            else:
                cur.execute(
                    "SELECT data FROM branding_clients ORDER BY created_at, id LIMIT %s OFFSET %s",
                    (limit, offset),
                )
            rows = cur.fetchall()
        return [Client.model_validate(r["data"]) for r in rows]

    @timed_query(store=_STORE, op="create_client")
    def create_client(
        self,
        name: str,
        contact_info: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Client:
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
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO branding_clients (id, data) VALUES (%s, %s)",
                (client_id, Json(client.model_dump(mode="json"))),
            )
        return client

    # ------------------------------------------------------------------
    # Brands
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="get_brand")
    def get_brand(self, client_id: str, brand_id: str) -> Optional[Brand]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT data FROM branding_brands WHERE id = %s AND client_id = %s",
                (brand_id, client_id),
            )
            row = cur.fetchone()
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
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM branding_brands WHERE id = %s LIMIT 1", (brand_id,))
            return cur.fetchone() is not None

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
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT client_id, data FROM branding_brands WHERE id = %s",
                (brand_id,),
            )
            row = cur.fetchone()
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
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, data FROM branding_brands WHERE id = ANY(%s)",
                (unique_ids,),
            )
            rows = cur.fetchall()
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
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            if limit is None:
                cur.execute(
                    "SELECT data FROM branding_brands WHERE client_id = %s ORDER BY created_at, id",
                    (client_id,),
                )
            else:
                cur.execute(
                    "SELECT data FROM branding_brands WHERE client_id = %s "
                    "ORDER BY created_at, id LIMIT %s OFFSET %s",
                    (client_id, limit, offset),
                )
            rows = cur.fetchall()
        return [Brand.model_validate(r["data"]) for r in rows]

    @timed_query(store=_STORE, op="create_brand")
    def create_brand(
        self,
        client_id: str,
        mission: BrandingMission,
        name: Optional[str] = None,
    ) -> Optional[Brand]:
        with get_conn() as conn, conn.cursor() as cur:
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

        Postconditions:
            Returns the updated Brand, or None when no such brand exists for
            the given client.
        """
        patch: dict = {"updated_at": _now_iso()}
        if mission is not None:
            patch["mission"] = mission.model_dump(mode="json")
        if status is not None:
            patch["status"] = status.value
        if name is not None:
            patch["name"] = name
        if conversation_id is not None:
            patch["conversation_id"] = conversation_id
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            return _apply_brand_patch(cur, brand_id, client_id, patch)

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
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
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

_default_store: Optional[BrandingStore] = None


def get_default_store() -> BrandingStore:
    """Return the process-wide store, instantiating on first call."""
    global _default_store
    if _default_store is None:
        _default_store = BrandingStore()
    return _default_store
