"""
Run blog pipeline with job store updates. Used by the API and by Temporal activities.
Accepts a request dict (serializable) so Temporal can pass it to activities.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from temporalio.exceptions import CancelledError

logger = logging.getLogger(__name__)


# Base directory for run artifacts (must match api/main.py RUN_ARTIFACTS_BASE when used from API)
def _get_run_artifacts_base() -> Path:
    import tempfile

    return Path(tempfile.gettempdir()) / "blogging_runs"


def _format_audience_from_dict(audience: Any) -> Optional[str]:
    """Format audience from request dict (str or dict with profession, skill_level, etc.)."""
    if audience is None:
        return None
    if isinstance(audience, str):
        return audience.strip() or None
    if isinstance(audience, dict):
        parts = []
        if audience.get("profession"):
            parts.append(f"profession: {audience['profession']}")
        if audience.get("skill_level"):
            parts.append(f"skill_level: {audience['skill_level']}")
        if audience.get("hobbies"):
            parts.append(f"interests: {', '.join(audience['hobbies'])}")
        if audience.get("other"):
            parts.append(audience["other"])
        return "; ".join(parts) if parts else None
    return None


def _is_external_cancellation(exc: BaseException) -> bool:
    """True when exception chain indicates runtime cancellation (e.g., Temporal)."""
    cur: Optional[BaseException] = exc
    for _ in range(8):
        if cur is None:
            break
        cls = cur.__class__
        if cls.__name__ == "CancelledError":
            module = getattr(cls, "__module__", "")
            if module.startswith("temporalio"):
                return True
        cur = cur.__cause__ or cur.__context__
    return False


def run_blog_full_pipeline_job(job_id: str, request_dict: Dict[str, Any]) -> None:
    """
    Run the full blog pipeline and update the job store. Used by API and Temporal activity.
    request_dict: brief, title_concept (optional), audience (str or dict), tone_or_purpose,
                  max_results, run_gates, max_rewrite_iterations,
                  content_profile, series_context, length_notes, target_word_count (all optional).
    """
    try:
        from agent_implementations.blog_writing_process_v2 import run_pipeline
        from blog_research_agent.models import ResearchBriefInput

        from shared.content_plan import content_plan_summary_text, content_plan_to_outline_markdown
        from shared.content_profile import resolve_length_policy_from_request_dict
    except ImportError:
        try:
            from blog_research_agent.models import ResearchBriefInput

            from blogging.agent_implementations.blog_writing_process_v2 import run_pipeline
            from blogging.shared.content_plan import content_plan_to_outline_markdown
            from blogging.shared.content_profile import resolve_length_policy_from_request_dict
        except ImportError as e:
            logger.exception("Import failed for pipeline job %s", job_id)
            _fail_job(job_id, str(e))
            return

    try:
        from blogging.shared.blog_job_store import (
            JOB_STATUS_CANCELLED,
            JOB_STATUS_COMPLETED,
            JOB_STATUS_NEEDS_REVIEW,
            complete_blog_job,
            fail_blog_job,
            start_blog_job,
            update_blog_job,
        )
        from blogging.shared.errors import BloggingError, PlanningError
    except ImportError:
        try:
            from shared.blog_job_store import (
                JOB_STATUS_CANCELLED,
                JOB_STATUS_COMPLETED,
                JOB_STATUS_NEEDS_REVIEW,
                complete_blog_job,
                fail_blog_job,  # noqa: F401
                start_blog_job,
                update_blog_job,
            )
            from shared.errors import BloggingError, PlanningError
        except ImportError:
            logger.warning("Blog job store not available; pipeline will run without job updates")
            update_blog_job = None
            start_blog_job = None
            complete_blog_job = None
            JOB_STATUS_CANCELLED = "cancelled"
            JOB_STATUS_COMPLETED = "completed"
            JOB_STATUS_NEEDS_REVIEW = "needs_human_review"
            BloggingError = Exception
            PlanningError = Exception

    work_dir = _get_run_artifacts_base() / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    brief_text = (request_dict.get("brief") or "").strip()
    if request_dict.get("title_concept"):
        brief_text = f"{brief_text}. Title concept: {request_dict['title_concept'].strip()}"
    audience_str = _format_audience_from_dict(request_dict.get("audience"))

    brief_input = ResearchBriefInput(
        brief=brief_text,
        audience=audience_str,
        tone_or_purpose=request_dict.get("tone_or_purpose"),
        max_results=int(request_dict.get("max_results", 20)),
    )

    def job_updater(**kwargs: Any) -> None:
        if update_blog_job is not None:
            try:
                update_blog_job(job_id, **kwargs)
            except CancelledError:
                raise
            except Exception as e:
                logger.warning("Failed to update job %s: %s", job_id, e)

    if start_blog_job is not None:
        start_blog_job(job_id)
    job_updater(work_dir=str(work_dir))

    stop_heartbeat = threading.Event()

    def _pipeline_heartbeat() -> None:
        """Keep last_heartbeat_at fresh during long LLM-heavy phases (no phase callback)."""
        while not stop_heartbeat.wait(30.0):
            if update_blog_job is not None:
                try:
                    update_blog_job(job_id)
                except Exception:
                    pass

    hb_thread: Optional[threading.Thread] = None
    if update_blog_job is not None:
        hb_thread = threading.Thread(
            target=_pipeline_heartbeat,
            name=f"blog-pipeline-hb-{job_id[:12]}",
            daemon=True,
        )
        hb_thread.start()

    try:
        length_policy = resolve_length_policy_from_request_dict(request_dict)
        _research_result, planning_phase_result, draft_result, status = run_pipeline(
            brief_input,
            work_dir=work_dir,
            run_gates=bool(request_dict.get("run_gates", True)),
            max_rewrite_iterations=int(request_dict.get("max_rewrite_iterations", 3)),
            job_updater=job_updater,
            length_policy=length_policy,
        )
        plan = planning_phase_result.content_plan
        outline = content_plan_to_outline_markdown(plan)
        title_choices = [
            {"title": tc.title, "probability_of_success": tc.probability_of_success}
            for tc in plan.title_candidates
        ]
        draft_preview = draft_result.draft[:2000] + (
            "..." if len(draft_result.draft) > 2000 else ""
        )
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
    except CancelledError:
        raise
    except PlanningError as e:
        logger.exception("Planning failed for job %s", job_id)
        _fail_job(
            job_id,
            str(e),
            failed_phase="planning",
            planning_failure_reason=getattr(e, "failure_reason", None),
        )
    except BloggingError as e:
        logger.exception("Pipeline failed for job %s", job_id)
        _fail_job(job_id, str(e), failed_phase=getattr(e, "phase", None))
    except Exception as e:
        if _is_external_cancellation(e):
            logger.info("Pipeline cancelled for job %s", job_id)
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
            return
        logger.exception("Unexpected error in pipeline for job %s", job_id)
        _fail_job(job_id, str(e))
    finally:
        stop_heartbeat.set()
        if hb_thread is not None:
            hb_thread.join(timeout=2.0)


def _fail_job(
    job_id: str,
    error: str,
    failed_phase: Optional[str] = None,
    planning_failure_reason: Optional[str] = None,
) -> None:
    try:
        from blogging.shared.blog_job_store import fail_blog_job as fn

        fn(
            job_id,
            error=error,
            failed_phase=failed_phase,
            planning_failure_reason=planning_failure_reason,
        )
    except ImportError:
        try:
            from shared.blog_job_store import fail_blog_job as fn

            fn(
                job_id,
                error=error,
                failed_phase=failed_phase,
                planning_failure_reason=planning_failure_reason,
            )
        except ImportError:
            pass
