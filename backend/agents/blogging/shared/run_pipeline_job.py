"""
Run blog pipeline with job store updates. Used by the API and by Temporal activities.
Accepts a request dict (serializable) so Temporal can pass it to activities.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from temporalio.exceptions import CancelledError

from agents.blogging.shared.audience import format_audience
from shared.concurrency import BackgroundHeartbeat

logger = logging.getLogger(__name__)


# Base directory for run artifacts (must match api/main.py RUN_ARTIFACTS_BASE when used from API).
# Resolution order (persistent first — /tmp is a last-resort fallback that
# does NOT survive container restarts):
#   1. $BLOGGING_RUN_ARTIFACTS_ROOT (explicit override)
#   2. $AGENT_CACHE/blogging_team/runs (shared volume convention)
#   3. tempfile.gettempdir()/blogging_runs (ephemeral — logs a loud warning)
_tempfile_fallback_warned = False


def _get_run_artifacts_base() -> Path:
    global _tempfile_fallback_warned
    import os
    import tempfile

    custom = os.environ.get("BLOGGING_RUN_ARTIFACTS_ROOT", "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    agent_cache = os.environ.get("AGENT_CACHE", "").strip()
    if agent_cache:
        return Path(agent_cache).expanduser().resolve() / "blogging_team" / "runs"
    fallback = Path(tempfile.gettempdir()) / "blogging_runs"
    if not _tempfile_fallback_warned:
        _tempfile_fallback_warned = True
        logger.warning(
            "Neither BLOGGING_RUN_ARTIFACTS_ROOT nor AGENT_CACHE is set — "
            "run artifacts will be written to %s, which is NOT persistent across "
            "process/container restarts. Set BLOGGING_RUN_ARTIFACTS_ROOT or AGENT_CACHE "
            "to a mounted volume for production deployments.",
            fallback,
        )
    return fallback


def _normalize_audience(audience: Any) -> Optional[str]:
    """Normalize an audience value (str, dict, or None) to a display string or None.

    Preconditions:
        - ``audience`` is a str, a dict (with optional profession/skill_level/hobbies/
          other keys), None, or any other value (coerced to None).
    Postconditions:
        - Returns a trimmed string, or None when the input is empty/unusable.
    """
    return format_audience(audience)


def _is_external_cancellation(exc: BaseException) -> bool:
    """True when the exception chain indicates a Temporal runtime cancellation.

    Walks the ``__cause__``/``__context__`` chain (bounded by a ``seen`` id-set so a
    self-referential chain can't loop forever) and tests each link with
    ``isinstance`` against ``temporalio.exceptions.CancelledError`` — robust to
    subclasses and free of the class-name/module string matching that a Temporal
    exception-hierarchy change could silently break.
    """
    cur: Optional[BaseException] = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, CancelledError):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _mark_cancelled_if_external(job_id: str, exc: BaseException) -> bool:
    """Short-circuit a terminal ``except`` arm when ``exc`` is an external cancellation.

    Centralizes the cancellation check shared verbatim by every terminal handler in
    ``run_blog_full_pipeline_job`` so the three arms don't each re-implement it.

    Preconditions:
        - ``job_id`` identifies the running job.
    Postconditions:
        - When ``_is_external_cancellation(exc)``: marks the job cancelled and
          returns True (the caller should ``return`` without failing the job).
        - Otherwise: no side effects, returns False.
    """
    if _is_external_cancellation(exc):
        mark_job_cancelled(job_id)
        return True
    return False


def _import_shared(name: str) -> Any:
    """Import ``agents.blogging.shared.<name>``.

    Single resolver so callers needing a ``shared`` submodule (kept dynamic to
    tolerate its absence in degraded/partial layouts) go through one place.

    Preconditions:
        - ``name`` is a module name under the blogging ``shared`` package
          (e.g. ``"blog_job_store"``).
    Postconditions:
        - Returns the imported module; propagates ImportError when it is
          unavailable.
    """
    import importlib

    return importlib.import_module(f"agents.blogging.shared.{name}")


def build_brief_input(request_dict: Dict[str, Any]) -> Any:
    """Build a ``ResearchBriefInput`` from a serialized request dict.

    Preconditions:
        - ``request_dict`` carries a ``brief`` string; ``title_concept``,
          ``audience`` (str or dict), ``tone_or_purpose``, and ``max_results`` are
          optional.
    Postconditions:
        - Returns a ``ResearchBriefInput`` with brief/audience/tone/max_results
          populated (title concept appended to the brief text when present).
    """
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    brief_text = (request_dict.get("brief") or "").strip()
    if request_dict.get("title_concept"):
        brief_text = f"{brief_text}. Title concept: {request_dict['title_concept'].strip()}"
    audience_str = _normalize_audience(request_dict.get("audience"))

    return ResearchBriefInput(
        brief=brief_text,
        audience=audience_str,
        tone_or_purpose=request_dict.get("tone_or_purpose"),
        max_results=int(request_dict.get("max_results", 20)),
    )


def _resolve_update_blog_job() -> Optional[Callable[..., Any]]:
    """Resolve ``update_blog_job`` across the package/sibling import paths (None if absent).

    Preconditions: none.
    Postconditions: returns the callable, or None when the job store is unavailable.
    """
    try:
        return _import_shared("blog_job_store").update_blog_job
    except ImportError:
        return None


def make_job_updater(job_id: str) -> Callable[..., None]:
    """Build the ``job_updater(**kwargs)`` callback for a pipeline run.

    Preconditions:
        - ``job_id`` identifies a created job record.
    Postconditions:
        - Returns a callable that writes ``kwargs`` to the job store (no-op when the
          store is unavailable) and broadcasts them to SSE subscribers. Re-raises
          CancelledError; swallows other job-update and SSE failures.
    """
    update_blog_job = _resolve_update_blog_job()

    def job_updater(**kwargs: Any) -> None:
        if update_blog_job is not None:
            try:
                update_blog_job(job_id, **kwargs)
            except CancelledError:
                raise
            except Exception as e:
                logger.warning("Failed to update job %s: %s", job_id, e)
        # Broadcast to SSE subscribers
        try:
            publish = _import_shared("job_event_bus").publish
        except ImportError:
            publish = None
        if publish is not None:
            try:
                publish(job_id, kwargs, event_type="update")
            except Exception:  # pragma: no cover - defensive guard around SSE bus; failures here must not break the pipeline.
                pass

    return job_updater


def start_pipeline_heartbeat(job_id: str) -> Optional[BackgroundHeartbeat]:
    """Start the background heartbeat that keeps the job (and Temporal activity) alive.

    Preconditions:
        - ``job_id`` identifies a running job.
    Postconditions:
        - Returns a started ``BackgroundHeartbeat`` (or None when the job store is
          unavailable) whose beat refreshes ``last_heartbeat_at`` and calls
          ``activity.heartbeat()`` when inside a Temporal activity.
    """
    update_blog_job = _resolve_update_blog_job()
    if update_blog_job is None:
        return None

    def _pipeline_beat() -> (
        None
    ):  # pragma: no cover - background thread driven by a 30s timer; exercising the beat body in unit tests requires faking threading + temporalio, which provides no signal beyond what targeted heartbeat tests already cover.
        """One heartbeat tick: keep last_heartbeat_at fresh and beat Temporal."""
        try:
            update_blog_job(job_id)
        except Exception:
            pass
        # Send Temporal activity heartbeat if running inside a Temporal activity.
        # RuntimeError means we're not in an activity context (e.g. local dev).
        try:
            from temporalio import activity as _act

            _act.heartbeat()
        except RuntimeError:
            pass

    # copy_context=True carries the Temporal activity ContextVar into the beater
    # thread; without it activity.heartbeat() silently no-ops (the ContextVar is
    # not auto-inherited by a new thread) and Temporal cancels the activity after
    # heartbeat_timeout expires.
    return BackgroundHeartbeat(
        _pipeline_beat,
        30.0,
        name=f"blog-pipeline-hb-{job_id[:12]}",
        copy_context=True,
        join_timeout=2.0,
    ).start()


def mark_job_cancelled(job_id: str) -> bool:
    """Mark a job as cancelled and publish the terminal SSE event.

    Preconditions:
        - ``job_id`` identifies a job whose run was cancelled (typically detected
          via ``_is_external_cancellation``).
    Postconditions:
        - The job store entry (when available) is set to CANCELLED and a terminal
          ``cancelled`` SSE event is published. Always returns True (for use in
          ``except`` handlers).
    """
    logger.info("Pipeline cancelled for job %s", job_id)
    try:
        JOB_STATUS_CANCELLED = _import_shared("blog_job_store").JOB_STATUS_CANCELLED
    except ImportError:
        JOB_STATUS_CANCELLED = "cancelled"
    update_blog_job = _resolve_update_blog_job()
    if update_blog_job is not None:
        try:
            update_blog_job(
                job_id,
                status=JOB_STATUS_CANCELLED,
                status_text="Pipeline cancelled",
                error="Cancelled",
            )
        except Exception:
            pass
    _publish_terminal(job_id, "cancelled")
    return True


def finalize_blog_job(
    job_id: str,
    planning_phase_result: Any,
    draft_result: Any,
    status: str,
) -> str:
    """Complete the job-store entry from a finished pipeline run.

    Shared by the thread-mode whole-run path (``run_blog_full_pipeline_job``) and the
    Temporal ``finalize`` activity so completion logic lives in one place.

    Preconditions:
        - ``planning_phase_result`` is a ``PlanningPhaseResult`` and ``draft_result``
          a ``WriterOutput`` from a completed run.
        - ``status`` is one of ``PASS`` / ``FAIL`` / ``NEEDS_HUMAN_REVIEW``.
    Postconditions:
        - Derives title choices/outline/draft preview/plan summary/planning metrics,
          calls ``complete_blog_job`` with COMPLETED (``status == "PASS"``) else
          NEEDS_REVIEW, and publishes a terminal ``complete`` SSE event. Returns the
          final job status string.
    """
    bjs = _import_shared("blog_job_store")
    content_plan = _import_shared("content_plan")
    JOB_STATUS_COMPLETED = bjs.JOB_STATUS_COMPLETED
    JOB_STATUS_NEEDS_REVIEW = bjs.JOB_STATUS_NEEDS_REVIEW
    complete_blog_job = bjs.complete_blog_job
    content_plan_summary_text = content_plan.content_plan_summary_text
    content_plan_to_outline_markdown = content_plan.content_plan_to_outline_markdown

    plan = planning_phase_result.content_plan
    outline = content_plan_to_outline_markdown(plan)
    title_choices = [
        {"title": tc.title, "probability_of_success": tc.probability_of_success}
        for tc in plan.title_candidates
    ]
    draft_preview = draft_result.draft
    final_status = JOB_STATUS_COMPLETED if status == "PASS" else JOB_STATUS_NEEDS_REVIEW
    if complete_blog_job is not None:
        complete_blog_job(
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
    _publish_terminal(job_id, "complete", status=final_status)
    return final_status


def run_blog_full_pipeline_job(job_id: str, request_dict: Dict[str, Any]) -> None:
    """Run the full blog pipeline and update the job store (thread-mode whole-run path).

    Also used by the legacy ``run_blog_full_pipeline`` Temporal activity retained for
    drain-out; the decomposed workflow uses the per-stage activities instead.

    Preconditions:
        - ``job_id`` identifies a job record already created in the job store.
        - ``request_dict`` carries a ``brief`` plus optional ``title_concept``,
          ``audience`` (str or dict), ``tone_or_purpose``, ``max_results``,
          ``run_gates``, ``max_rewrite_iterations``, ``content_profile``,
          ``series_context``, ``length_notes``, ``target_word_count``.
    Postconditions:
        - The job is started, run to completion, and terminalized in the job store:
          ``finalize_blog_job`` on success, or (via ``_fail_job`` /
          ``mark_job_cancelled``) failed/cancelled on error. Never raises except to
          re-propagate a Temporal ``CancelledError``; all other errors are recorded
          on the job and swallowed.
    """
    try:
        from agents.blogging.agent_implementations.blog_writing_process_v2 import run_pipeline
        from agents.blogging.shared.content_profile import resolve_length_policy_from_request_dict
    except ImportError as e:
        logger.exception("Import failed for pipeline job %s", job_id)
        _fail_job(job_id, str(e))
        return

    try:
        start_blog_job = _import_shared("blog_job_store").start_blog_job
        _errors = _import_shared("errors")
        BloggingError = _errors.BloggingError
        PlanningError = _errors.PlanningError
    except ImportError:
        logger.warning("Blog job store not available; pipeline will run without job updates")
        start_blog_job = None
        BloggingError = Exception
        PlanningError = Exception

    work_dir = _get_run_artifacts_base() / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    brief_input = build_brief_input(request_dict)
    job_updater = make_job_updater(job_id)

    if start_blog_job is not None:
        start_blog_job(job_id)
    job_updater(work_dir=str(work_dir))

    hb = start_pipeline_heartbeat(job_id)

    try:
        length_policy = resolve_length_policy_from_request_dict(request_dict)
        planning_phase_result, draft_result, status = run_pipeline(
            brief_input,
            work_dir=work_dir,
            run_gates=bool(request_dict.get("run_gates", True)),
            max_rewrite_iterations=int(request_dict.get("max_rewrite_iterations", 3)),
            job_updater=job_updater,
            job_id=job_id,
            length_policy=length_policy,
        )
        finalize_blog_job(job_id, planning_phase_result, draft_result, status)
    except CancelledError:
        raise
    except PlanningError as e:
        if _mark_cancelled_if_external(job_id, e):
            return
        logger.exception("Planning failed for job %s", job_id)
        _fail_job(
            job_id,
            str(e),
            failed_phase="planning",
            planning_failure_reason=getattr(e, "failure_reason", None),
        )
        _publish_terminal(job_id, "error", error=str(e), failed_phase="planning")
    except BloggingError as e:
        if _mark_cancelled_if_external(
            job_id, e
        ):  # pragma: no cover - extremely narrow race-only branch; the cancellation path is already exercised via the PlanningError variant above.
            return
        logger.exception("Pipeline failed for job %s", job_id)
        _fail_job(job_id, str(e), failed_phase=getattr(e, "phase", None))
        _publish_terminal(job_id, "error", error=str(e), failed_phase=getattr(e, "phase", None))
    except Exception as e:
        # Broad by design: this is the terminal funnel for the thread-mode run. The
        # job is marked failed and a traceback is logged at ERROR (logger.exception)
        # so unexpected AttributeError/TypeError etc. are still surfaced in logs — the
        # swallow only prevents the worker thread from dying silently.
        if _mark_cancelled_if_external(job_id, e):
            return
        logger.exception("Unexpected error in pipeline for job %s", job_id)
        _fail_job(job_id, str(e))
        _publish_terminal(job_id, "error", error=str(e))
    finally:
        if hb is not None:
            hb.stop()


def _publish_terminal(job_id: str, event_type: str, **kwargs: Any) -> None:
    """Publish a terminal SSE event and clean up subscribers.

    Preconditions:
        - ``job_id`` identifies the finished job; ``event_type`` is a terminal SSE
          type (``complete``/``error``/``cancelled``) and ``kwargs`` its payload.
    Postconditions:
        - Best-effort only — never raises. Silently no-ops when the event-bus
          module is unavailable or the publish/cleanup itself fails, so callers
          must not rely on the terminal event actually being delivered.
    """
    try:
        bus = _import_shared("job_event_bus")
    except ImportError:
        return
    try:
        bus.publish(job_id, kwargs, event_type=event_type)
        bus.cleanup_job(job_id)
    except Exception:  # pragma: no cover - defensive guard around SSE bus.
        pass


def _fail_job(
    job_id: str,
    error: str,
    failed_phase: Optional[str] = None,
    planning_failure_reason: Optional[str] = None,
) -> None:
    """Mark a job as failed in the job store.

    Preconditions:
        - ``job_id`` identifies a created job record; ``error`` is the failure
          message; ``failed_phase``/``planning_failure_reason`` are optional
          attribution fields.
    Postconditions:
        - ``fail_blog_job`` has recorded the failure — or, when the job-store
          module is unavailable (degraded layout), the failure record is silently
          dropped and this function no-ops. Store errors raised by the call itself
          propagate to the caller.
    """
    try:
        fn = _import_shared("blog_job_store").fail_blog_job
    except ImportError:
        return
    fn(
        job_id,
        error=error,
        failed_phase=failed_phase,
        planning_failure_reason=planning_failure_reason,
    )
