"""Blogging API — Medium post statistics collection (sync + async)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from agents.blogging.api.models import StartPipelineResponse
from agents.blogging.blog_medium_stats_agent.models import (
    MediumStatsReport,
    MediumStatsRunConfig,
)
from agents.blogging.shared.medium_stats_api import MediumStatsRequest
from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_medium_integration() -> None:
    from agents.blogging.api import main as _main

    ok, msg = _main.medium_stats_integration_eligible()
    if not ok:
        raise HTTPException(status_code=503, detail=msg)


def _run_medium_stats_async_job(job_id: str, payload: MediumStatsRequest) -> None:
    """Background worker: scrape Medium stats and write medium_stats_report.json."""
    from agents.blogging.api import main as _main

    if _main._job_already_terminal(job_id):
        logger.info("Skipping Medium stats job %s: already terminal/gone before start", job_id)
        _main._publish_skip_terminal_event(job_id)
        return
    cfg = MediumStatsRunConfig(
        headless=payload.headless,
        timeout_ms=payload.timeout_ms,
        max_posts=payload.max_posts,
    )
    try:
        ok, msg = _main.medium_stats_integration_eligible()
        if not ok:
            raise RuntimeError(msg)
        if _main.start_blog_job is not None:
            _main.start_blog_job(job_id)
        if (
            _main.get_blog_job is None
        ):  # pragma: no cover - defensive guard for the ImportError fallback at module import; tests always have the blog_job_store bound.
            raise RuntimeError("Job store unavailable")
        job = _main.get_blog_job(job_id)
        work_dir_str = job.get("work_dir") if job else None
        if not work_dir_str:  # pragma: no cover - reached only when the job row is missing the work_dir we just set; covered by integration tests.
            raise RuntimeError("Medium stats job missing work_dir")
        if _main.update_blog_job is not None:
            _main.update_blog_job(
                job_id,
                status_text="Collecting Medium statistics…",
                progress=15,
                phase="medium_stats",
            )
        report = _main.BlogMediumStatsAgent().collect(cfg)
        if (
            _main.write_artifact is None
        ):  # pragma: no cover - defensive guard for the ImportError fallback at module import; tests always have shared.artifacts bound.
            raise RuntimeError("Artifact persistence not available")
        _main.write_artifact(
            work_dir_str, "medium_stats_report.json", report.model_dump(mode="json")
        )
        if _main.update_blog_job is not None:
            _main.update_blog_job(
                job_id,
                status=_main.JOB_STATUS_COMPLETED,
                phase="medium_stats",
                progress=100,
                status_text=f"Collected statistics for {len(report.posts)} posts",
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        logger.info("Completed Medium stats job %s (%s posts)", job_id, len(report.posts))
    except Exception as e:
        logger.exception("Medium stats failed for job %s", job_id)
        if _main.fail_blog_job is not None:
            _main.fail_blog_job(job_id, error=str(e), failed_phase="medium_stats")


@router.post(
    "/medium-stats",
    response_model=MediumStatsReport,
    summary="Collect Medium post statistics (sync)",
    description=(
        "Runs Playwright against medium.com/me/stats. "
        "Requires the Medium.com integration: enabled and configured under /api/integrations/medium "
        "(Google OAuth identity when using Google, plus an imported Playwright browser session)."
    ),
)
def medium_stats_sync(payload: MediumStatsRequest) -> MediumStatsReport:
    """Synchronous Medium statistics scrape."""
    from agents.blogging.api import main as _main

    _require_medium_integration()
    cfg = MediumStatsRunConfig(
        headless=payload.headless,
        timeout_ms=payload.timeout_ms,
        max_posts=payload.max_posts,
    )
    try:
        return _main.BlogMediumStatsAgent().collect(cfg)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        logger.exception("Medium stats sync failed")
        raise HTTPException(status_code=500, detail=f"Medium stats failed: {e}") from e


@router.post(
    "/medium-stats-async",
    response_model=StartPipelineResponse,
    summary="Start Medium statistics collection asynchronously",
    description=(
        "Creates a job with job_type medium_stats and work_dir under medium_stats_runs. "
        "Poll GET /job/{job_id}; artifact medium_stats_report.json when status is completed."
    ),
)
def medium_stats_async(payload: MediumStatsRequest) -> StartPipelineResponse:
    """Start Medium stats job in a background thread."""
    from agents.blogging.api import main as _main

    if _main.create_blog_job is None or _main.medium_stats_run_dir is None:
        raise HTTPException(
            status_code=501,
            detail="Job store not available for async Medium stats",
        )
    _require_medium_integration()
    job_id = str(uuid.uuid4())[:8]
    work_dir = str(_main.medium_stats_run_dir(job_id))
    _main.create_blog_job(
        job_id,
        brief="Medium post statistics",
        work_dir=work_dir,
        job_type="medium_stats",
    )
    _main._submit_async_job(_run_medium_stats_async_job, job_id, payload)
    logger.info("Started async Medium stats job %s", job_id)
    return StartPipelineResponse(job_id=job_id, message="Medium statistics job started")
