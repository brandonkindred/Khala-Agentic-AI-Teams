"""Background run/job orchestration for the branding team API.

Holds the bounded-executor + Temporal-dispatch machinery that drives brand runs
off the request thread, plus the job lifecycle transitions. The collaborators
tests monkeypatch (``orchestrator``, ``branding_store``, ``_run_executor``,
``_job_manager``, ``_job_heartbeat``) are owned by ``main`` and dereferenced
through it at call time.

The ``import main as _main`` is done **inside** each function that needs it,
not at module scope. ``main`` re-exports names from this module at its own bottom
(``_run_branding_core`` et al. for the test surface), so a module-scope
``from branding_team.api import main`` here would form a load-time cycle:
importing ``background`` first would trigger ``main``, which re-imports names
from a still-partially-initialised ``background``. Keeping the hub import
function-local lets ``background`` (and its consumers) be imported independently.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, List, NoReturn, Optional
from uuid import uuid4

from fastapi import HTTPException

from branding_team.api.models import RunBrandJobResponse, RunBrandRequest
from branding_team.models import BrandCheckRequest, BrandingMission, BrandPhase, HumanReview
from branding_team.shared.job_store import (
    JOB_STATUS_PENDING,
    begin_job,
    create_job,
    mark_completed,
    mark_failed,
)
from branding_team.shared.phase_output_cache import PhaseOutputCache

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
    from branding_team.api import main as _main

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
        - On a genuine failure, attempts to mark the row FAILED via
          ``mark_failed`` and then **re-raises the original exception**
          regardless of whether that write succeeds — even if ``mark_failed``
          itself raises, that secondary error is logged and swallowed rather
          than replacing the original one. Callers (the Temporal activity) can
          rely on the original exception surfacing as a failed workflow, but
          should not assume the row was actually written FAILED.
    """
    from branding_team.api import main as _main

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
                phase_cache=_main._get_brand_cache(brand_id) if brand_id else PhaseOutputCache(),
            )
        mark_completed(job_id, result.model_dump())
    except Exception as e:
        logger.exception("Branding job %s failed", job_id)
        try:
            marked_failed = mark_failed(job_id, str(e))
        except Exception:
            # mark_failed's own failure (e.g. JobNotFoundError) must never
            # replace the original pipeline exception — that would mask the
            # real cause behind an unrelated bookkeeping error.
            logger.exception("Branding job %s: mark_failed itself failed", job_id)
            raise e from None
        if not marked_failed:
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

    Note:
        Calls ``_main._run_branding_core`` (the re-exported binding on the hub),
        not the module-local ``_run_branding_core`` — ``main`` re-exports this
        function specifically so tests can intercept it
        (``patch.object(main_mod, "_run_branding_core", ...)``); calling the
        local name here would make that patch a silent no-op.
    """
    from branding_team.api import main as _main

    try:
        _main._run_branding_core(
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


def _temporal_enabled() -> bool:
    """Report whether Temporal dispatch is available, tolerating its absence.

    Lazy import keeps main.py's import cost low and defers the Pattern A worker
    boot in ``branding_team.temporal`` until the first dispatch.

    Preconditions:
        None.
    Postconditions:
        Returns ``is_temporal_enabled()`` when ``shared.temporal`` imports;
        returns ``False`` when that import fails (Temporal not installed in this
        deployment) rather than propagating the ``ImportError``.
    """
    try:
        from shared.temporal import is_temporal_enabled
    except ImportError:
        return False
    return is_temporal_enabled()


def _fail_job_and_raise_503(job_id: str, reason: str) -> NoReturn:
    """Mark a dispatched job failed and raise a 503, never surfacing a 500.

    Shared by both dispatch paths: a dispatch failure should read to the client
    as "service unavailable", and a secondary failure of the bookkeeping write
    must not replace that intended outcome.

    Preconditions:
        ``job_id`` refers to a job row already created via ``create_job``;
        ``reason`` is the failure message to record.
    Postconditions:
        Always raises ``HTTPException(503)``. First attempts
        ``mark_failed(job_id, reason)``; if that itself raises, the error is
        logged and swallowed so the 503 is not masked by a leaked 500. Never
        returns.
    """
    try:
        mark_failed(job_id, reason)
    except Exception:
        # Bookkeeping failure must not replace the intended 503 — the client
        # should see "unavailable", not a leaked 500.
        logger.exception("Branding job %s: mark_failed itself failed", job_id)
    raise HTTPException(status_code=503, detail="Service temporarily unavailable")


def _dispatch_temporal(
    job_id: str,
    brand: Any,
    human_review: HumanReview,
    payload: RunBrandRequest,
    client_id: str,
    brand_id: str,
    target_phase: Optional[BrandPhase],
) -> None:
    """Dispatch a branding job as a durable Temporal workflow.

    The run is visible in the Temporal UI; an orphaned run after a restart is
    reconciled to ``interrupted`` by the team_service startup recovery rather
    than lost.

    Preconditions:
        ``_temporal_enabled()`` returned True; ``job_id`` refers to a created
        job row; ``brand`` exposes a ``mission`` model.
    Postconditions:
        Returns ``None`` after ``start_branding_workflow`` accepts a fully
        serialized payload. On any dispatch error (client/worker not ready), the
        error is logged and the job is failed via ``_fail_job_and_raise_503`` —
        so this raises ``HTTPException(503)`` instead of a 500.
    """
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
        logger.exception("Branding job %s Temporal dispatch failed", job_id)
        _fail_job_and_raise_503(job_id, "temporal dispatch failed")


def _dispatch_thread(
    job_id: str,
    brand: Any,
    human_review: HumanReview,
    payload: RunBrandRequest,
    client_id: str,
    brand_id: str,
    target_phase: Optional[BrandPhase],
) -> None:
    """Dispatch a branding job to the in-process bounded thread pool.

    Preconditions:
        ``_temporal_enabled()`` returned False; ``job_id`` refers to a created
        job row; ``brand`` exposes a ``mission`` model.
    Postconditions:
        Returns ``None`` after submitting ``_run_branding_background`` to the run
        executor. If the executor was shut down (``RuntimeError``, e.g. app
        teardown), the job is failed via ``_fail_job_and_raise_503`` — so this
        raises ``HTTPException(503)`` instead of letting the ``RuntimeError``
        surface as a 500.
    """
    from branding_team.api import main as _main

    try:
        # Submit the hub's re-exported binding (not the module-local name) so a
        # test that patches main._run_branding_background to intercept a
        # thread-path submission actually observes it — same reasoning as
        # _run_branding_background calling _main._run_branding_core above.
        _main._run_executor.submit(
            _main._run_branding_background,
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
        _fail_job_and_raise_503(job_id, "run executor unavailable")


def _submit_brand_run(
    client_id: str,
    brand_id: str,
    payload: RunBrandRequest,
    target_phase: Optional[BrandPhase],
) -> RunBrandJobResponse:
    """Create a job row and dispatch a branding run, Temporal or thread-pool.

    Preconditions:
        ``client_id``/``brand_id`` identify a brand; ``payload`` is the parsed
        run request; ``target_phase`` is the single phase to run, or ``None``
        for the full pipeline.
    Postconditions:
        Returns ``RunBrandJobResponse(job_id, status=JOB_STATUS_PENDING)`` after
        creating the job row and dispatching it (durable workflow when
        ``_temporal_enabled()``, else the bounded thread pool). Raises
        ``HTTPException(404)`` when the brand is missing, and ``HTTPException(503)``
        when dispatch fails (see ``_dispatch_temporal`` / ``_dispatch_thread``).
    """
    from branding_team.api import main as _main

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

    if _temporal_enabled():
        _dispatch_temporal(job_id, brand, human_review, payload, client_id, brand_id, target_phase)
    else:
        _dispatch_thread(job_id, brand, human_review, payload, client_id, brand_id, target_phase)
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
        from shared.temporal import is_temporal_enabled

        if not is_temporal_enabled():
            return
        from branding_team.temporal.constants import WORKFLOW_ID_PREFIX
        from shared.temporal import signal_workflow_sync

        # client_ready_timeout_s=0 so the cancel endpoint never blocks waiting for
        # the worker client — the signal is only an optimization (the job-store
        # cancel flag already stops the run at the next phase boundary), so if the
        # worker isn't immediately reachable we skip it rather than hang the request.
        signal_workflow_sync(f"{WORKFLOW_ID_PREFIX}{job_id}", "cancel", client_ready_timeout_s=0)
    except Exception:
        logger.debug("branding cancel signal not delivered for job %s", job_id, exc_info=True)
