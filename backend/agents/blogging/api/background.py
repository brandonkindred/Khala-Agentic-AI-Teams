"""Background drivers shared by the pipeline, medium_stats, and jobs routers.

``_run_pipeline_with_tracking`` is the async /full-pipeline-async job body — also
re-dispatched by the jobs router's resume/restart routes. ``_publish_terminal_event``/
``_publish_skip_terminal_event`` are the SSE-stream terminal-event helpers shared by
the pipeline and medium_stats background jobs. ``_import_run_pipeline``/
``_prepare_pipeline_input`` are small helpers shared by the sync and async pipeline
routes.

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
from agents.blogging.shared.content_plan import (
    content_plan_summary_text,
    content_plan_to_outline_markdown,
)
from agents.blogging.shared.content_profile import LengthPolicy, resolve_length_policy
from agents.blogging.shared.errors import PlanningError

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


def _run_pipeline_with_tracking(
    job_id: str, request: FullPipelineRequest
) -> None:  # pragma: no cover - background-thread pipeline driver; depends on the v2 orchestrator (which is itself omitted from coverage as an agent_implementations script) and on live job-store + SSE side effects. Hot paths are exercised end-to-end by integration tests; the request-validation and error-handling branches at the API boundary are covered by the synchronous /full-pipeline tests.
    """Run the full pipeline in a background thread with job tracking."""
    from agents.blogging.api import main as _main

    if _main._job_already_terminal(job_id):
        logger.info("Skipping pipeline job %s: already terminal/gone before start", job_id)
        _main._publish_skip_terminal_event(job_id)
        return
    try:
        run_pipeline = _import_run_pipeline()

        work_dir = _main.RUN_ARTIFACTS_BASE / job_id
        work_dir.mkdir(parents=True, exist_ok=True)

        brief_input, length_policy = _prepare_pipeline_input(request)

        def job_updater(**kwargs: Any) -> None:
            """Update job status in the job store and broadcast to SSE subscribers."""
            if _main.update_blog_job is not None:
                try:
                    _main.update_blog_job(job_id, **kwargs)
                except Exception as e:
                    logger.warning("Failed to update job %s: %s", job_id, e)
            try:
                from agents.blogging.shared.job_event_bus import publish

                publish(job_id, kwargs, event_type="update")
            except Exception:
                pass

        # Mark job as started
        if _main.start_blog_job is not None:
            _main.start_blog_job(job_id)
        job_updater(work_dir=str(work_dir))

        try:
            planning_phase_result, draft_result, status = run_pipeline(
                brief_input,
                work_dir=work_dir,
                run_gates=request.run_gates,
                max_rewrite_iterations=request.max_rewrite_iterations,
                job_updater=job_updater,
                job_id=job_id,
                length_policy=length_policy,
            )

            plan = planning_phase_result.content_plan
            outline = content_plan_to_outline_markdown(plan)
            title_choices = [
                {"title": tc.title, "probability_of_success": tc.probability_of_success}
                for tc in plan.title_candidates
            ]
            draft_preview = draft_result.draft

            final_status = (
                _main.JOB_STATUS_COMPLETED if status == "PASS" else _main.JOB_STATUS_NEEDS_REVIEW
            )
            if _main.complete_blog_job is not None:
                _main.complete_blog_job(
                    job_id,
                    status=final_status,
                    title_choices=title_choices,
                    outline=outline,
                    draft_preview=draft_preview,
                    content_plan_summary=content_plan_summary_text(plan),
                    planning_iterations_used=planning_phase_result.planning_iterations_used,
                    parse_retry_count=planning_phase_result.parse_retry_count,
                    planning_wall_ms_total=planning_phase_result.planning_wall_ms_total,
                )
            _main._publish_terminal_event(job_id, "complete", status=final_status)

        except PlanningError as e:
            logger.exception("Pipeline planning failed for job %s", job_id)
            if _main.fail_blog_job is not None:
                _main.fail_blog_job(
                    job_id,
                    error=str(e),
                    failed_phase="planning",
                    planning_failure_reason=getattr(e, "failure_reason", None),
                )
            _main._publish_terminal_event(job_id, "error", error=str(e), failed_phase="planning")
        except _main.BloggingError as e:
            logger.exception("Pipeline failed for job %s", job_id)
            if _main.fail_blog_job is not None:
                _main.fail_blog_job(job_id, error=str(e), failed_phase=getattr(e, "phase", None))
            _main._publish_terminal_event(job_id, "error", error=str(e))
        except Exception as e:
            logger.exception("Unexpected error in pipeline for job %s", job_id)
            if _main.fail_blog_job is not None:
                _main.fail_blog_job(job_id, error=str(e))
            _main._publish_terminal_event(job_id, "error", error=str(e))
    except Exception as e:
        logger.exception("Pipeline failed for job %s", job_id)
        if _main.fail_blog_job is not None:
            _main.fail_blog_job(job_id, error=str(e))
        _main._publish_terminal_event(job_id, "error", error=str(e))
