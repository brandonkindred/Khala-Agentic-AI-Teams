"""Postgres-backed store for prospect dossiers and ranked prospect lists.

Data is persisted in the shared Khala Postgres instance via
``shared.postgres.get_conn``. DDL lives in ``sales_team.postgres`` and is
registered from the team's FastAPI lifespan.

Every public method is wrapped in ``@timed_query`` so slow reads and writes
surface as structured log lines.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Json

from shared.postgres import get_conn
from shared.postgres.metrics import timed_query

from .models import DeepResearchResult, ProspectDossier

logger = logging.getLogger(__name__)

_STORE = "sales"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, defaulting to now() on failure."""
    if not value:
        return datetime.now(tz=timezone.utc)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(tz=timezone.utc)


class DossierStore:
    """Persists ProspectDossier + DeepResearchResult records.

    Stateless — the Postgres pool is owned by ``shared.postgres`` and reads
    the ``POSTGRES_*`` env vars. Intended to be instantiated per-request.
    """

    def __init__(self) -> None:
        # Stateless; shared.postgres owns the pool.
        pass

    # ------------------------------------------------------------------
    # Dossiers
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="save_dossier")
    def save_dossier(self, dossier: ProspectDossier) -> ProspectDossier:
        """Insert a dossier, assigning ``dossier_id`` and ``generated_at`` if empty."""
        if not dossier.dossier_id:
            dossier.dossier_id = f"dsr_{uuid4().hex[:12]}"
        if not dossier.generated_at:
            dossier.generated_at = _now_iso()
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO sales_dossiers
                       (id, prospect_id, company_name, full_name, data, generated_at)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET
                       prospect_id = EXCLUDED.prospect_id,
                       company_name = EXCLUDED.company_name,
                       full_name = EXCLUDED.full_name,
                       data = EXCLUDED.data,
                       generated_at = EXCLUDED.generated_at
                """,
                (
                    dossier.dossier_id,
                    dossier.prospect_id,
                    dossier.current_company,
                    dossier.full_name,
                    Json(dossier.model_dump(mode="json")),
                    _parse_ts(dossier.generated_at),
                ),
            )
        return dossier

    @timed_query(store=_STORE, op="get_dossier")
    def get_dossier(self, dossier_id: str) -> Optional[ProspectDossier]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT data FROM sales_dossiers WHERE id = %s", (dossier_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return ProspectDossier.model_validate(row["data"])

    @timed_query(store=_STORE, op="get_dossiers_by_prospect_ids")
    def get_dossiers_by_prospect_ids(self, prospect_ids: List[str]) -> Dict[str, ProspectDossier]:
        """Batch-load dossiers keyed by their linked prospect_id.

        Used by the outreach loop so we do one query per pipeline run instead
        of one per prospect. Prospect ids without a dossier are simply absent
        from the returned map — callers decide how to handle misses.
        """
        if not prospect_ids:
            return {}
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT prospect_id, data FROM sales_dossiers WHERE prospect_id = ANY(%s)",
                (list(prospect_ids),),
            )
            rows = cur.fetchall()
        out: Dict[str, ProspectDossier] = {}
        for row in rows:
            dossier = ProspectDossier.model_validate(row["data"])
            # If multiple dossiers exist for a prospect, keep the newest.
            existing = out.get(row["prospect_id"])
            if existing is None or dossier.generated_at > existing.generated_at:
                out[row["prospect_id"]] = dossier
        return out

    # ------------------------------------------------------------------
    # Prospect lists
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="save_prospect_list")
    def save_prospect_list(self, result: DeepResearchResult) -> DeepResearchResult:
        """Insert a ranked prospect list, assigning ``list_id``/``generated_at`` if empty."""
        if not result.list_id:
            result.list_id = f"plst_{uuid4().hex[:12]}"
        if not result.generated_at:
            result.generated_at = _now_iso()
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO sales_prospect_lists
                       (id, product_name, total_prospects, companies_represented,
                        data, generated_at)
                   VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    result.list_id,
                    result.product_name,
                    result.total_prospects,
                    result.companies_represented,
                    Json(result.model_dump(mode="json")),
                    _parse_ts(result.generated_at),
                ),
            )
        return result

    @timed_query(store=_STORE, op="get_prospect_list")
    def get_prospect_list(self, list_id: str) -> Optional[DeepResearchResult]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT data FROM sales_prospect_lists WHERE id = %s", (list_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return DeepResearchResult.model_validate(row["data"])

    @timed_query(store=_STORE, op="list_prospect_lists")
    def list_prospect_lists(self, limit: int = 50) -> List[dict]:
        """Return lightweight summaries of the most recent prospect lists."""
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT id, product_name, total_prospects, companies_represented, generated_at
                   FROM sales_prospect_lists
                   ORDER BY generated_at DESC
                   LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [
            {
                "list_id": r["id"],
                "product_name": r["product_name"],
                "total_prospects": r["total_prospects"],
                "companies_represented": r["companies_represented"],
                "generated_at": (
                    r["generated_at"].isoformat()
                    if isinstance(r["generated_at"], datetime)
                    else str(r["generated_at"])
                ),
            }
            for r in rows
        ]
