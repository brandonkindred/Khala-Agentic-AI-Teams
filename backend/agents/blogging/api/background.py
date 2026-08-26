"""Background drivers shared by the pipeline, medium_stats, and jobs routers.

``_run_pipeline_with_tracking`` is the async /full-pipeline-async job body — also
re-dispatched by the jobs router's resume/restart routes. It delegates the actual
pipeline run to ``run_blog_full_pipeline_job`` (the same entry point the legacy
Temporal activity uses). ``_publish_terminal_event``/``_publish_skip_terminal_event``
are the SSE-stream terminal-event helpers shared by the pipeline and medium_stats
background jobs. ``_import_run_pipeline``/``_prepare_pipeline_input`` are small
helpers used by the sync pipeline route.

Every collaborator here that ``api.main`` re-exports (and that the test suite
monkeypatches on ``main`` — ``RUN_ARTIFACTS_BASE``, the blog_job_store helpers,
``BloggingError``, ``_job_already_terminal``, ``_publish_terminal_event``) is
dereferenced via a late ``from agents.blogging.api import main as _main`` import
inside each function body, never captured at module load, so
``monkeypatch.setattr(main, "X", ...)`` in tests keeps working after this code moved
out of ``main.py``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Tuple

from agents.blogging.api.models import FullPipelineRequest, _format_audience
from agents.blogging.blog_research_agent.models import ResearchBriefInput
from agents.blogging.shared.content_profile import LengthPolicy, resolve_length_policy

logger = logging.getLogger(__name__)


def _import_run_pipeline() -> Callable[..., Any]:
    """Lazily import and return the v2 pipeline orchestrator.

    Deliberately lazy to avoid a heavy import at module load.
    """
    from agents.blogging.agent_implementations.blog_writing_process_v2 import run_pipeline

    return run_pipeline


def _prepare_pipeline_input(
    request: FullPipelineRequest,
) -> Tuple[ResearchBriefInput, LengthPolicy]:
    """Build the ``ResearchBriefInput`` and ``LengthPolicy`` shared by both pipeline runners.

    Preconditions: ``request`` is a valid ``FullPipelineRequest``.
    Postconditions: returns ``(brief_input, length_policy)`` derived purely from
        ``request`` fields (no filesystem or job-store access).
    """
    brief_text = request.brief.strip()
    if request.title_concept:
        brief_text = f"{brief_text}. Title concept: {request.title_concept.strip()}"
    audience_str = _format_audience(request.audience)

    brief_input = ResearchBriefInput(
        brief=brief_text,
        audience=audience_str or None,
        tone_or_purpose=request.tone_or_purpose,
        max_results=request.max_results,
    )
    length_policy = resolve_length_policy(
        content_profile=request.content_profile,
        explicit_target_word_count=request.target_word_count,
        length_notes=request.length_notes,
        series_context=request.series_context,
    )
    return brief_input, length_policy


def _publish_terminal_event(job_id: str, event_type: str, **kwargs: Any) -> None:
    """Publish a terminal SSE event and clean up subscribers."""
    try:
        from agents.blogging.shared.job_event_bus import cleanup_job, publish

        publish(job_id, kwargs, event_type=event_type)
        cleanup_job(job_id)
    except Exception:
        pass


def _publish_skip_terminal_event(job_id: str) -> None:
    """Publish the terminal SSE event for a queued job a worker is skipping.

    When ``_job_already_terminal`` skips a job, a client that subscribed to
    ``/job/{job_id}/stream`` while the job was still ``pending`` would otherwise keep
    receiving keepalives until the stream deadline: the stream only closes on a terminal
    bus event (``complete``/``error``/``cancelled``), and the transition that made the job
    terminal (stale-monitor fail, user cancel) does not itself publish one. Emit the
    matching event so the stream closes promptly.

    Best-effort — never raises. An ``interrupted`` job is only ever produced by the
    shutdown hook (the event bus is being torn down alongside the process, and
    ``interrupted`` is not a stream-terminal status), and a missing/unreadable job has no
    meaningful subscriber, so those emit nothing.
    """
    from agents.blogging.api import main as _main

    if _main.get_blog_job is None:
        return
    try:
        job = _main.get_blog_job(job_id)
    except Exception:
        return
    status = job.get("status") if job else None
    if status == _main.JOB_STATUS_CANCELLED:
        _main._publish_terminal_event(job_id, "cancelled", status=status)
    elif status == _main.JOB_STATUS_FAILED:
        _main._publish_terminal_event(
            job_id,
            "error",
            status=status,
            error=(job or {}).get("error") or "Job failed before it started.",
        )


def _run_pipeline_with_tracking(job_id: str, request: FullPipelineRequest) -> None:
    """Run the full pipeline in a background thread with job tracking."""
    from agents.blogging.api import main as _main
    from agents.blogging.shared.run_pipeline_job import run_blog_full_pipeline_job

    if _main._job_already_terminal(job_id):
        logger.info("Skipping pipeline job %s: already terminal/gone before start", job_id)
        _main._publish_skip_terminal_event(job_id)
        return

    try:
        request_dict = request.model_dump(mode="json")
        audience_str = _format_audience(request.audience)
        request_dict["audience"] = audience_str or request_dict.get("audience")
        run_blog_full_pipeline_job(job_id, request_dict)
    except Exception as e:
        logger.exception("Pipeline failed for job %s", job_id)
        if _main.fail_blog_job is not None:
            _main.fail_blog_job(job_id, error=str(e))
        _main._publish_terminal_event(job_id, "error", error=str(e))
