"""Postgres-backed store for clients and brands with versioning.

Data is persisted in the shared Khala Postgres instance via
``shared_postgres.get_conn``. DDL lives in ``branding_team.postgres`` and
is registered from the team's FastAPI lifespan.

Every public method is wrapped in ``@timed_query`` so slow reads and
writes surface as structured log lines.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Json

from shared_postgres import get_conn, merge_jsonb_via_cursor
from shared_postgres.metrics import timed_query

from .models import (
    Brand,
    BrandingMission,
    BrandStatus,
    Client,
    TeamOutput,
)

logger = logging.getLogger(__name__)

_STORE = "branding"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


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
    def list_clients(self) -> List[Client]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT data FROM branding_clients")
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

    @timed_query(store=_STORE, op="get_brand_by_id")
    def get_brand_by_id(self, brand_id: str) -> Optional[Brand]:
        """Return the brand with this globally-unique id, regardless of owner.

        Brand ids are globally unique (``brand_`` + a uuid4 slug), so at most
        one row matches. Lets callers resolve a brand from its id alone instead
        of scanning every client (the old ``list_clients`` → ``get_brand`` N+1).

        Preconditions:
            - ``brand_id`` is a brand identifier string.
        Postconditions:
            - Returns the single matching :class:`Brand`, or ``None`` when no
              row has that id. Issues exactly one query.
        """
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT data FROM branding_brands WHERE id = %s", (brand_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return Brand.model_validate(row["data"])

    @timed_query(store=_STORE, op="list_brands_for_client")
    def list_brands_for_client(self, client_id: str) -> List[Brand]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT data FROM branding_brands WHERE client_id = %s",
                (client_id,),
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
        """Patch a brand's top-level fields atomically and return the new state.

        Replaces the former read-modify-write cycle (SELECT → ``model_validate``
        the whole doc → mutate → rewrite the whole blob) with a single
        server-side shallow merge (``data || patch``). One round-trip, no
        reserialize of unchanged fields, and concurrency-safe: two callers
        patching disjoint fields no longer clobber each other.

        Preconditions:
            - ``client_id``/``brand_id`` identify a brand (else ``None`` is
              returned).
        Postconditions:
            - Only the supplied fields (plus ``updated_at``) are changed; all
              other stored fields are preserved.
            - Returns the merged :class:`Brand`, or ``None`` when no row matched.
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
            return merge_jsonb_via_cursor(
                cur,
                "branding_brands",
                key={"id": brand_id, "client_id": client_id},
                patch=patch,
                model=Brand,
            )

    # Atomic version bump + history append + field merge, all server-side.
    # The version counter and the history entry's version are both derived from
    # the row's current ``version`` in the same statement, so concurrent appends
    # can never collide on a version number or lose a history entry (the former
    # read-modify-write rewrote the whole — ever-growing — ``history`` array in
    # Python and raced on ``version``).
    _APPEND_VERSION_SQL = (
        "UPDATE branding_brands SET data = jsonb_set("
        "jsonb_set("
        "data || %s::jsonb, "
        "'{version}', to_jsonb(COALESCE((data->>'version')::int, 0) + 1)"
        "), "
        "'{history}', COALESCE(data->'history', '[]'::jsonb) || jsonb_build_array("
        "jsonb_build_object("
        "'version', COALESCE((data->>'version')::int, 0) + 1, "
        "'created_at', %s::text, "
        "'status', %s::text"
        "))"
        ") WHERE id = %s AND client_id = %s RETURNING data"
    )

    @timed_query(store=_STORE, op="append_brand_version")
    def append_brand_version(
        self,
        client_id: str,
        brand_id: str,
        output: TeamOutput,
    ) -> Optional[Brand]:
        """Append a new version to a brand atomically and return the new state.

        Performs the whole update in a single server-side statement: bumps
        ``version``, appends one :class:`BrandVersionSummary` to ``history``,
        and merges ``latest_output``/``current_phase``/``updated_at``. The
        history array is concatenated in Postgres, so the (unbounded, growing)
        prior history is never pulled into Python and rewritten.

        Preconditions:
            - ``client_id``/``brand_id`` identify a brand (else ``None``).
        Postconditions:
            - ``version`` increases by exactly 1; exactly one history entry is
              appended carrying the new version, ``output.status`` and the
              write timestamp.
            - Returns the updated :class:`Brand`, or ``None`` when no row matched.
        """
        now = _now_iso()
        current_phase = output.current_phase.value if output.current_phase is not None else None
        field_patch = {
            "latest_output": output.model_dump(mode="json"),
            "current_phase": current_phase,
            "updated_at": now,
        }
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                self._APPEND_VERSION_SQL,
                (Json(field_patch), now, output.status.value, brand_id, client_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Brand.model_validate(row["data"])


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
