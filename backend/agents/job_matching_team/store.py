"""Postgres-backed store for job matching runs and ranked results.

Pure data access: opens short-lived pool-backed connections through
``shared_postgres.get_conn``. DDL lives in ``job_matching_team.postgres`` and
is registered from the team's FastAPI lifespan. Every public method is wrapped
in ``@timed_query`` so slow reads/writes surface as structured log lines.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from psycopg.rows import dict_row
from psycopg.types.json import Json

from shared_postgres import get_conn
from shared_postgres.metrics import timed_query

from .models import (
    LISTING_FILTERS,
    JobMatchRequest,
    JobPosting,
    Listing,
    ListingsResponse,
    ListingStateUpdate,
    RankedJob,
    RunDetail,
    RunSummary,
    SubScores,
)
from .profile.model import JobSeekerProfile

logger = logging.getLogger(__name__)

_STORE = "job_matching"

# Job run statuses (mirrors the central job-service vocabulary).
RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


#: Aggregated-listing SELECT: latest ranked snapshot per fingerprint (DISTINCT ON,
#: newest first), how often the posting was ranked, and the user's triage state
#: (implicit ``new`` when no state row exists). Callers append a WHERE/ORDER
#: BY/LIMIT tail. Trusted literal only — never interpolate untrusted input.
_LISTING_SELECT = (
    "WITH latest AS ("
    " SELECT DISTINCT ON (fingerprint) fingerprint, run_id, score, sub_scores,"
    "  posting, recommendation, rationale, concerns, created_at"
    " FROM job_matching_ranked_jobs WHERE fingerprint <> ''"
    " ORDER BY fingerprint, created_at DESC, id DESC"
    "), seen AS ("
    " SELECT fingerprint, COUNT(*) AS times_seen"
    " FROM job_matching_ranked_jobs WHERE fingerprint <> '' GROUP BY fingerprint"
    ") "
    "SELECT l.fingerprint, l.run_id, l.score, l.sub_scores, l.posting,"
    " l.recommendation, l.rationale, l.concerns, l.created_at, seen.times_seen,"
    " COALESCE(s.status, 'new') AS status, s.notes, s.updated_at AS status_updated_at "
    "FROM latest l "
    "JOIN seen USING (fingerprint) "
    "LEFT JOIN job_matching_listing_states s USING (fingerprint)"
)


def _listing_filter_clause(status: str) -> tuple[str, tuple]:
    """Return the WHERE tail + params for a validated listing filter.

    Preconditions:
        * ``status`` is one of :data:`LISTING_FILTERS` (caller-asserted).
    Postconditions:
        * ``all`` → no filtering; ``active`` → hides ``archived`` and
          ``not_interested``; any single status → exact match.
    """
    if status == "all":
        return "", ()
    if status == "active":
        return " WHERE COALESCE(s.status, 'new') NOT IN ('archived', 'not_interested')", ()
    return " WHERE COALESCE(s.status, 'new') = %s", (status,)


def _listing_from_row(row: dict) -> Listing:
    """Build a :class:`Listing` from an aggregated-listing row dict.

    Preconditions:
        * ``row`` contains every column selected by :data:`_LISTING_SELECT`.
    Postconditions:
        * Timestamps are ISO-rendered; JSON columns are model-validated.
    """
    return Listing(
        fingerprint=row["fingerprint"],
        posting=JobPosting.model_validate(row["posting"]),
        score=float(row["score"]),
        sub_scores=SubScores.model_validate(row["sub_scores"]),
        recommendation=row["recommendation"],
        rationale=row["rationale"],
        concerns=list(row["concerns"] or []),
        run_id=row["run_id"],
        last_seen_at=_iso(row["created_at"]),
        times_seen=int(row["times_seen"] or 1),
        status=row["status"],
        notes=row["notes"],
        status_updated_at=_iso(row["status_updated_at"]),
    )


def _iso(value: object) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


class JobMatchingStore:
    """Persistence for scan runs and their ranked postings.

    Invariants:
        * A ``job_matching_ranked_jobs`` row always references an existing
          ``job_matching_runs.run_id`` (writes go through :meth:`save_results`,
          which is only called after :meth:`create_run`).
    """

    def __init__(self) -> None:
        # Stateless; the connection pool lives inside shared_postgres.
        pass

    @timed_query(store=_STORE, op="create_run")
    def create_run(self, run_id: str, profile: JobSeekerProfile, request: JobMatchRequest) -> None:
        """Insert a new run row in ``running`` state.

        Preconditions:
            * ``run_id`` is unique (caller generates a UUID).
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO job_matching_runs "
                "(run_id, status, profile_snapshot, request_json, top_n, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    run_id,
                    RUN_STATUS_RUNNING,
                    Json(profile.model_dump(mode="json")),
                    Json(request.model_dump(mode="json")),
                    request.top_n,
                    _now(),
                ),
            )

    @timed_query(store=_STORE, op="save_results")
    def save_results(
        self,
        run_id: str,
        ranked: List[RankedJob],
        *,
        total_found: int,
        scanned_fingerprints: Optional[List[str]] = None,
    ) -> None:
        """Persist ranked rows and mark the run completed.

        Args:
            ranked: The returned (top-N) ranked postings; one row each.
            total_found: Count of all postings scanned this run.
            scanned_fingerprints: Fingerprints of *every* posting scanned this
                run (not just the returned top-N). Persisted on the run row so
                ``exclude_seen`` can suppress lower-ranked roles already seen.

        Postconditions:
            * The run's ``status`` is ``completed`` with ``total_found`` /
              ``total_ranked`` populated and ``completed_at`` set.
            * One ``job_matching_ranked_jobs`` row exists per entry in
              ``ranked`` (rank starts at 1, in list order).
            * The run's ``seen_fingerprints`` holds the de-duplicated set of
              ``scanned_fingerprints`` (falling back to the ranked postings'
              fingerprints when not supplied).
        """
        if scanned_fingerprints is None:
            scanned_fingerprints = [rj.posting.fingerprint for rj in ranked]
        seen = sorted({fp for fp in scanned_fingerprints if fp})
        now = _now()
        with get_conn() as conn, conn.cursor() as cur:
            for idx, rj in enumerate(ranked, start=1):
                cur.execute(
                    "INSERT INTO job_matching_ranked_jobs "
                    "(run_id, rank, score, sub_scores, posting, recommendation, "
                    " rationale, concerns, fingerprint, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        run_id,
                        idx,
                        rj.score,
                        Json(rj.sub_scores.model_dump(mode="json")),
                        Json(rj.posting.model_dump(mode="json")),
                        rj.recommendation,
                        rj.rationale,
                        Json(rj.concerns),
                        rj.posting.fingerprint,
                        now,
                    ),
                )
            cur.execute(
                "UPDATE job_matching_runs SET status = %s, total_found = %s, "
                "total_ranked = %s, seen_fingerprints = %s, completed_at = %s WHERE run_id = %s",
                (RUN_STATUS_COMPLETED, total_found, len(ranked), Json(seen), now, run_id),
            )

    @timed_query(store=_STORE, op="mark_failed")
    def mark_failed(self, run_id: str, error: str) -> None:
        """Mark a run failed.

        Postconditions:
            * The stored ``error`` is capped at 2000 characters so an unbounded
              exception dump cannot bloat the run row.
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE job_matching_runs SET status = %s, error = %s, completed_at = %s "
                "WHERE run_id = %s",
                (RUN_STATUS_FAILED, (error or "")[:2000], _now(), run_id),
            )

    @timed_query(store=_STORE, op="list_runs")
    def list_runs(self, *, limit: int = 50) -> List[RunSummary]:
        """Return the most recent run summaries, newest first."""
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT run_id, status, total_found, total_ranked, created_at, completed_at "
                "FROM job_matching_runs ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            return [
                RunSummary(
                    run_id=r["run_id"],
                    status=r["status"],
                    total_found=int(r["total_found"] or 0),
                    total_ranked=int(r["total_ranked"] or 0),
                    created_at=_iso(r["created_at"]),
                    completed_at=_iso(r["completed_at"]),
                )
                for r in cur.fetchall()
            ]

    @timed_query(store=_STORE, op="get_run")
    def get_run(self, run_id: str) -> Optional[RunDetail]:
        """Return a run plus its ranked jobs, or None if unknown."""
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT run_id, status, total_found, total_ranked, error, created_at, completed_at "
                "FROM job_matching_runs WHERE run_id = %s",
                (run_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                "SELECT score, sub_scores, posting, recommendation, rationale, concerns "
                "FROM job_matching_ranked_jobs WHERE run_id = %s ORDER BY rank",
                (run_id,),
            )
            ranked = [
                RankedJob(
                    posting=JobPosting.model_validate(j["posting"]),
                    score=float(j["score"]),
                    sub_scores=SubScores.model_validate(j["sub_scores"]),
                    recommendation=j["recommendation"],
                    rationale=j["rationale"],
                    concerns=list(j["concerns"] or []),
                )
                for j in cur.fetchall()
            ]
        return RunDetail(
            run_id=row["run_id"],
            status=row["status"],
            total_found=int(row["total_found"] or 0),
            total_ranked=int(row["total_ranked"] or 0),
            error=row["error"],
            created_at=_iso(row["created_at"]),
            completed_at=_iso(row["completed_at"]),
            ranked_jobs=ranked,
        )

    @timed_query(store=_STORE, op="list_listings")
    def list_listings(self, *, status: str = "active", limit: int = 200) -> ListingsResponse:
        """Return aggregated listings (latest snapshot per fingerprint) plus counts.

        Preconditions:
            * ``status`` is one of :data:`LISTING_FILTERS` (API validates; a
              violation here is a caller bug).
            * ``limit`` is a positive int.
        Postconditions:
            * Each fingerprint appears at most once, carrying its most recent
              ranked snapshot; results are ordered ``score DESC``.
            * ``counts`` maps every present status (incl. implicit ``new``) to
              its fingerprint count, regardless of the active filter.
        """
        assert status in LISTING_FILTERS, f"invalid listing filter: {status!r}"
        assert limit >= 1, "limit must be positive"
        where, params = _listing_filter_clause(status)
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                _LISTING_SELECT + where + " ORDER BY l.score DESC LIMIT %s",
                (*params, limit),
            )
            listings = [_listing_from_row(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT COALESCE(s.status, 'new') AS status, COUNT(*) AS n "
                "FROM (SELECT DISTINCT fingerprint FROM job_matching_ranked_jobs "
                "      WHERE fingerprint <> '') f "
                "LEFT JOIN job_matching_listing_states s USING (fingerprint) "
                "GROUP BY 1"
            )
            counts = {r["status"]: int(r["n"]) for r in cur.fetchall()}
        return ListingsResponse(listings=listings, total=len(listings), counts=counts)

    @timed_query(store=_STORE, op="update_listing_state")
    def update_listing_state(
        self, fingerprint: str, update: ListingStateUpdate
    ) -> Optional[Listing]:
        """Upsert the user state for ``fingerprint`` and return the fresh listing.

        Preconditions:
            * ``fingerprint`` is a non-empty string; ``update`` is validated.
        Postconditions:
            * Returns ``None`` (and writes nothing) when no ranked posting with
              that fingerprint exists — the API maps this to 404.
            * Otherwise exactly one ``job_matching_listing_states`` row exists
              for the fingerprint with the new ``status``; ``notes=None`` on
              the update leaves previously stored notes unchanged.
        """
        assert fingerprint, "fingerprint must be non-empty"
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT 1 FROM job_matching_ranked_jobs WHERE fingerprint = %s LIMIT 1",
                (fingerprint,),
            )
            if cur.fetchone() is None:
                return None
            cur.execute(
                "INSERT INTO job_matching_listing_states (fingerprint, status, notes, updated_at) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (fingerprint) DO UPDATE SET "
                "status = EXCLUDED.status, "
                "notes = COALESCE(EXCLUDED.notes, job_matching_listing_states.notes), "
                "updated_at = EXCLUDED.updated_at",
                (fingerprint, update.status, update.notes, _now()),
            )
            cur.execute(
                _LISTING_SELECT + " WHERE l.fingerprint = %s",
                (fingerprint,),
            )
            row = cur.fetchone()
        # Postcondition: the existence check passed, so the snapshot row exists.
        assert row is not None, f"listing snapshot missing for fingerprint {fingerprint!r}"
        return _listing_from_row(row)

    @timed_query(store=_STORE, op="seen_fingerprints")
    def seen_fingerprints(self) -> set[str]:
        """Return every posting fingerprint scanned in any prior run.

        Postconditions:
            * Includes fingerprints from the run-level ``seen_fingerprints``
              arrays (every posting scanned, not just the returned top-N) as
              well as the per-row ranked-job fingerprints, so ``exclude_seen``
              suppresses lower-ranked roles already encountered.
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT jsonb_array_elements_text(seen_fingerprints) "
                "FROM job_matching_runs"
            )
            seen = {r[0] for r in cur.fetchall() if r[0]}
            cur.execute(
                "SELECT DISTINCT fingerprint FROM job_matching_ranked_jobs WHERE fingerprint <> ''"
            )
            seen.update(r[0] for r in cur.fetchall())
            return seen


# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------

_default_store: Optional[JobMatchingStore] = None


def get_store() -> JobMatchingStore:
    """Return the process-wide store, instantiating on first call."""
    global _default_store
    if _default_store is None:
        _default_store = JobMatchingStore()
    return _default_store
