"""Background run/job orchestration for the branding team API.

Holds the bounded-executor + Temporal-dispatch machinery that drives brand runs
off the request thread, plus the job lifecycle transitions. The collaborators
tests monkeypatch (``orchestrator``, ``branding_store``, ``_run_executor``,
``_job_manager``, ``_job_heartbeat``) are owned by ``main`` and dereferenced
through it at call time via ``_main`` — this module is imported at the bottom of
``main`` (after those globals + ``app`` are defined), so ``_main`` binds a
fully-populated hub and ``monkeypatch.setattr(main, …)`` keeps working.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional
from uuid import uuid4

from fastapi import HTTPException

from branding_team.api import main as _main
from branding_team.api.models import RunBrandJobResponse, RunBrandRequest
from branding_team.models import BrandCheckRequest, BrandingMission, BrandPhase, HumanReview
from branding_team.shared.job_store import (
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    begin_job,
    create_job,
    mark_completed,
    mark_failed,
    update_job,
)

logger = logging.getLogger(__name__)


async def _run_in_pipeline_executor(func, *args):
    """Await *func(*args)* on the bounded ``_run_executor`` (not the loop's default).

    Preconditions:
        ``func`` is a synchronous callable that may run a branding pipeline
        (or sub-pipeline); ``args`` are its positional arguments.
    Postconditions:
        Returns ``func(*args)``'s result, or propagates whatever it raises.

    Note:
        Deliberately routed through ``_run_executor`` — the same bounded pool
        used for job-tracked pipeline runs — rather than ``asyncio.to_thread``,
        which uses the process-wide default executor shared by any other code
        in this (single, multi-team) process calling ``asyncio.to_thread`` /
        ``loop.run_in_executor(None, ...)``. A handful of concurrent, multi-minute
        pipeline runs on the shared default executor could starve unrelated
        async-offloaded work elsewhere in the app; keeping pipeline work on its
        own bounded pool avoids that.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_main._run_executor, func, *args)


def _run_branding_core(
    job_id: str,
    mission: BrandingMission,
    human_review: HumanReview,
    brand_checks: List[BrandCheckRequest],
    client_id: Optional[str],
    brand_id: Optional[str],
    include_market_research: bool,
    include_design_assets: bool,
    target_phase: Optional[BrandPhase],
) -> None:
    """Run the branding pipeline for ``job_id`` and record job status.

    Used by the thread path (via ``_run_branding_background``). The Temporal
    activity path (``temporal.activities``) runs the same pipeline as separate
    durable activities, but both paths drive their RUNNING/COMPLETED/FAILED
    transitions through the same guarded helpers (``begin_job``/
    ``mark_completed``/``mark_failed`` in ``branding_team.shared.job_store``),
    so the cancel-check + status-write sequence lives in exactly one place.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.
    Postconditions:
        - On success the job row ends COMPLETED with the serialized
          ``TeamOutput``.
        - If the job was cancelled, leaves the row as-is and returns (a
          cancelled run is terminal, not a failure).
        - On a genuine failure, marks the row FAILED and **re-raises the
          original exception** so callers (the Temporal activity) can surface it
          as a failed workflow rather than a silently-"completed" one.
    """
    try:
        if not begin_job(job_id):
            return
        # orchestrator.run has no progress callback, so it never touches the job
        # heartbeat itself. Drive it from a background beater for the duration of the
        # (potentially multi-minute) run so the stale-job monitor doesn't fail a live run.
        with _main._job_heartbeat(job_id):
            result = _main.orchestrator.run(
                mission=mission,
                human_review=human_review,
                brand_checks=brand_checks,
                store=_main.branding_store if (client_id and brand_id) else None,
                client_id=client_id,
                brand_id=brand_id,
                include_market_research=include_market_research,
                include_design_assets=include_design_assets,
                target_phase=target_phase,
            )
        mark_completed(job_id, result.model_dump())
    except Exception as e:
        logger.exception("Branding job %s failed", job_id)
        if not mark_failed(job_id, str(e)):
            return
        raise


def _run_branding_background(
    job_id: str,
    mission: BrandingMission,
    human_review: HumanReview,
    brand_checks: List[BrandCheckRequest],
    client_id: Optional[str],
    brand_id: Optional[str],
    include_market_research: bool,
    include_design_assets: bool,
    target_phase: Optional[BrandPhase],
) -> None:
    """Thread-path wrapper around ``_run_branding_core`` that swallows failures.

    The core already logs and writes the FAILED job row; this wrapper is what
    the ``_run_executor`` submits, so it must not let the exception escape into
    an unretrieved ``Future`` (the caller never awaits it).

    Postconditions:
        - Never raises. Job status is written by ``_run_branding_core``.
    """
    try:
        _run_branding_core(
            job_id,
            mission,
            human_review,
            brand_checks,
            client_id,
            brand_id,
            include_market_research,
            include_design_assets,
            target_phase,
        )
    except Exception:
        # Already logged + FAILED row written by the core; the thread path is
        # fire-and-forget, so absorb it here.
        pass


def _submit_brand_run(
    client_id: str,
    brand_id: str,
    payload: RunBrandRequest,
    target_phase: Optional[BrandPhase],
) -> RunBrandJobResponse:
    brand = _main.branding_store.get_brand(client_id, brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    human_review = HumanReview(approved=payload.human_approved, feedback=payload.human_feedback)
    job_id = str(uuid4())
    create_job(
        job_id,
        client_id=client_id,
        brand_id=brand_id,
        current_phase=target_phase.value if target_phase else None,
    )

    # When Temporal is enabled, dispatch the job as a durable workflow (visible
    # in the Temporal UI; an orphaned run after a restart is reconciled to
    # ``interrupted`` by the team_service startup recovery rather than lost);
    # otherwise fall back to the in-process thread pool. Lazy import keeps
    # main.py's import cost low and defers the Pattern A worker boot in
    # branding_team.temporal until the first dispatch.
    try:
        from shared_temporal import is_temporal_enabled

        temporal_on = is_temporal_enabled()
    except ImportError:
        temporal_on = False

    if temporal_on:
        from branding_team.temporal.start_workflow import start_branding_workflow

        wf_payload = {
            "job_id": job_id,
            "mission": brand.mission.model_dump(),
            "human_review": human_review.model_dump(),
            "brand_checks": [c.model_dump() for c in payload.brand_checks],
            "client_id": client_id,
            "brand_id": brand_id,
            "include_market_research": payload.include_market_research,
            "include_design_assets": payload.include_design_assets,
            "target_phase": target_phase.value if target_phase else None,
        }
        try:
            start_branding_workflow(job_id, wf_payload)
        except Exception:
            # Temporal client/worker not ready — fail the job row and return 503
            # rather than surfacing the dispatch error as a 500.
            logger.exception("Branding job %s Temporal dispatch failed", job_id)
            update_job(job_id, status=JOB_STATUS_FAILED, error="temporal dispatch failed")
            raise HTTPException(status_code=503, detail="Service temporarily unavailable")
        return RunBrandJobResponse(job_id=job_id, status=JOB_STATUS_PENDING)

    try:
        _main._run_executor.submit(
            _run_branding_background,
            job_id,
            brand.mission,
            human_review,
            payload.brand_checks,
            client_id,
            brand_id,
            payload.include_market_research,
            payload.include_design_assets,
            target_phase,
        )
    except RuntimeError:
        # Executor was shut down (e.g. app teardown) — fail the job row and
        # return 503 rather than letting the RuntimeError surface as a 500.
        update_job(job_id, status=JOB_STATUS_FAILED, error="run executor unavailable")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")
    return RunBrandJobResponse(job_id=job_id, status=JOB_STATUS_PENDING)


def _signal_branding_cancel(job_id: str) -> None:
    """Best-effort: deliver the ``cancel`` signal to a running BrandingWorkflow.

    The job-store cancel flag is the source of truth (honored cooperatively at
    each phase boundary by ``check_branding_cancelled_activity``); this signal
    just makes the cancel Temporal-native so it is observed without waiting on the
    next between-phase job-service poll. Any failure (Temporal disabled, worker
    unavailable, workflow already gone) is swallowed — the flag still stops the run.
    """
    try:
        from shared_temporal import is_temporal_enabled

        if not is_temporal_enabled():
            return
        from branding_team.temporal.constants import WORKFLOW_ID_PREFIX
        from shared_temporal import signal_workflow_sync

        # client_ready_timeout_s=0 so the cancel endpoint never blocks waiting for
        # the worker client — the signal is only an optimization (the job-store
        # cancel flag already stops the run at the next phase boundary), so if the
        # worker isn't immediately reachable we skip it rather than hang the request.
        signal_workflow_sync(f"{WORKFLOW_ID_PREFIX}{job_id}", "cancel", client_ready_timeout_s=0)
    except Exception:
        logger.debug("branding cancel signal not delivered for job %s", job_id, exc_info=True)
