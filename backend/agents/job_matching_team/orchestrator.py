"""Top-level controller for the job matching pipeline.

Sequence: load + merge profile -> build queries -> scan the web -> rank ->
persist the run and ranked results -> return the top-N as a response.
"""

from __future__ import annotations

import logging
from typing import Optional
from uuid import uuid4

from llm_service import llm_attribution

from .agents.query_builder import QueryBuilderAgent
from .agents.ranker import JobRankerAgent
from .agents.scanner import JobScannerAgent
from .models import JobMatchRequest, JobMatchResponse
from .profile.loader import load_job_seeker_profile
from .profile.model import JobSeekerProfile

logger = logging.getLogger(__name__)


class JobMatchingOrchestrator:
    """Coordinate query building, scanning, ranking, and persistence.

    Agents and the store are injectable so the whole pipeline runs offline in
    tests with mock LLM/search/fetch and no Postgres.

    Invariants:
        * When ``persist`` is enabled and a store is available, every call to
          :meth:`run` records exactly one run row (completed on success, failed
          on exception).
    """

    def __init__(
        self,
        *,
        query_builder: Optional[QueryBuilderAgent] = None,
        scanner: Optional[JobScannerAgent] = None,
        ranker: Optional[JobRankerAgent] = None,
        store: Optional[object] = None,
        persist: bool = True,
    ) -> None:
        self._query_builder = query_builder or QueryBuilderAgent()
        self._scanner = scanner or JobScannerAgent()
        self._ranker = ranker or JobRankerAgent()
        self._store = store
        self._persist = persist

    def _get_store(self):  # noqa: ANN202
        if self._store is None and self._persist:
            from .store import get_store

            self._store = get_store()
        return self._store

    def run(
        self,
        request: JobMatchRequest,
        *,
        profile: Optional[JobSeekerProfile] = None,
        job_id: Optional[str] = None,
    ) -> JobMatchResponse:
        """Execute one scan-and-rank run and return the ranked top-N.

        ``job_id`` is the owning API/job-service identifier (e.g. the id returned
        by ``POST /scan``); when provided it is used for LLM telemetry attribution
        so operators can correlate records with the identifier they hold. When
        omitted (direct/sync callers) the internal ``run_id`` is used instead.

        Preconditions:
            * ``request`` is a valid :class:`JobMatchRequest`.
        Postconditions:
            * ``response.ranked_jobs`` is sorted best-first and has at most
              ``request.top_n`` entries.
            * ``response.total_found`` >= ``response.total_ranked``.
        """
        base = profile if profile is not None else load_job_seeker_profile()
        effective = base.merged_with(request.profile_overrides)

        run_id = str(uuid4())
        store = self._get_store()
        if store is not None:
            try:
                store.create_run(run_id, effective, request)
            except Exception:  # noqa: BLE001 - persistence must not abort the scan
                logger.warning("Failed to create run row %s", run_id, exc_info=True)
                store = None

        try:
            skip = set()
            if request.exclude_seen and store is not None:
                try:
                    skip = store.seen_fingerprints()
                except Exception:  # noqa: BLE001
                    logger.warning("seen_fingerprints lookup failed", exc_info=True)

            # Stamp the team + owning job on every LLM call this run makes; the
            # per-agent objective/agent_key are bound at each call site. Prefer
            # the API job_id (what operators search by); fall back to run_id.
            with llm_attribution(team="job_matching", job_id=job_id or run_id):
                queries = self._query_builder.build(effective, max_queries=request.max_queries)
                postings = self._scanner.scan(
                    queries, max_roles=request.max_roles, skip_fingerprints=skip
                )
                ranked = self._ranker.rank(postings, effective)
                top = ranked[: request.top_n]

            if store is not None:
                try:
                    store.save_results(
                        run_id,
                        top,
                        total_found=len(postings),
                        scanned_fingerprints=[p.fingerprint for p in postings],
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("Failed to save results for run %s", run_id, exc_info=True)
                    # Don't leave the run stuck in RUNNING; record the persistence
                    # failure so it isn't mistaken for an in-flight scan.
                    try:
                        store.mark_failed(run_id, "persisting results failed")
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "Failed to mark run %s failed after save error", run_id, exc_info=True
                        )

            return JobMatchResponse(
                run_id=run_id,
                ranked_jobs=top,
                total_found=len(postings),
                total_ranked=len(top),
                profile_snapshot=effective,
            )
        except Exception as exc:
            if store is not None:
                try:
                    store.mark_failed(run_id, str(exc))
                except Exception:  # noqa: BLE001
                    logger.warning("Failed to mark run %s failed", run_id, exc_info=True)
            raise
