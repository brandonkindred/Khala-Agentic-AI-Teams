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

from shared_postgres import get_conn
from shared_postgres.metrics import timed_query

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
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT data FROM branding_brands WHERE id = %s AND client_id = %s",
                (brand_id, client_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            brand = Brand.model_validate(row["data"])
            updates: dict = {"updated_at": _now_iso()}
            if mission is not None:
                updates["mission"] = mission
            if status is not None:
                updates["status"] = status
            if name is not None:
                updates["name"] = name
            if conversation_id is not None:
                updates["conversation_id"] = conversation_id
            updated = brand.model_copy(update=updates)
            cur.execute(
                "UPDATE branding_brands SET data = %s WHERE id = %s AND client_id = %s",
                (Json(updated.model_dump(mode="json")), brand_id, client_id),
            )
        return updated

    @timed_query(store=_STORE, op="append_brand_version")
    def append_brand_version(
        self,
        client_id: str,
        brand_id: str,
        output: TeamOutput,
    ) -> Optional[Brand]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT data FROM branding_brands WHERE id = %s AND client_id = %s",
                (brand_id, client_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            brand = Brand.model_validate(row["data"])
            now = _now_iso()
            new_version = brand.version + 1
            history_entry = BrandVersionSummary(
                version=new_version,
                created_at=now,
                status=output.status.value,
            )
            updated = brand.model_copy(
                update={
                    "latest_output": output,
                    "current_phase": output.current_phase,
                    "version": new_version,
                    "history": list(brand.history) + [history_entry],
                    "updated_at": now,
                }
            )
            cur.execute(
                "UPDATE branding_brands SET data = %s WHERE id = %s AND client_id = %s",
                (Json(updated.model_dump(mode="json")), brand_id, client_id),
            )
        return updated


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
