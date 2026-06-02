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
    JobMatchRequest,
    JobPosting,
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
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE job_matching_runs SET status = %s, error = %s, completed_at = %s "
                "WHERE run_id = %s",
                (RUN_STATUS_FAILED, error[:2000], _now(), run_id),
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
