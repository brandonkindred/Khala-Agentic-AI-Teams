"""
FastAPI application exposing the full blogging pipeline (planning -> draft -> gates).

Supports synchronous and asynchronous execution with job polling and SSE streaming for UI integration.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict

from agents.blogging.blog_medium_stats_agent.agent import (  # noqa: E402,F401
    BlogMediumStatsAgent,
)
from agents.blogging.postgres import SCHEMA as BLOGGING_POSTGRES_SCHEMA  # noqa: E402
from agents.blogging.shared.artifacts import (  # noqa: E402,F401
    ARTIFACT_NAMES,
    ARTIFACT_PRODUCER,
    read_artifact,
    write_artifact,
)
from agents.blogging.shared.errors import BloggingError  # noqa: E402
from agents.blogging.shared.medium_integration_access import (  # noqa: F401
    medium_stats_integration_eligible,  # noqa: E402
)
from pydantic import BaseModel  # noqa: E402

from shared.app import create_team_app  # noqa: E402

try:
    from agents.blogging.shared.blog_job_store import (
        JOB_STATUS_CANCELLED,
        JOB_STATUS_COMPLETED,
        JOB_STATUS_FAILED,
        JOB_STATUS_NEEDS_REVIEW,
        approve_blog_job,
        complete_blog_job,
        create_blog_job,
        delete_blog_job,
        fail_blog_job,
        get_blog_job,
        is_waiting_for_draft_feedback,
        list_blog_jobs,
        medium_stats_run_dir,
        skip_current_story_gap,
        start_blog_job,
        submit_blog_answers,
        submit_draft_feedback,
        submit_story_user_message,
        submit_title_ratings,
        submit_title_selection,
        unapprove_blog_job,
        update_blog_job,
    )
except ImportError:  # pragma: no cover - defensive ImportError fallback for environments without the shared blog_job_store module; not exercised in tests because conftest guarantees the import path resolves.
    create_blog_job = None
    delete_blog_job = None
    get_blog_job = None
    list_blog_jobs = None
    update_blog_job = None
    start_blog_job = None
    complete_blog_job = None
    fail_blog_job = None
    approve_blog_job = None
    unapprove_blog_job = None
    medium_stats_run_dir = None
    submit_title_selection = None
    submit_title_ratings = None
    submit_story_user_message = None
    skip_current_story_gap = None
    submit_blog_answers = None
    submit_draft_feedback = None
    is_waiting_for_draft_feedback = None
    JOB_STATUS_COMPLETED = "completed"
    JOB_STATUS_NEEDS_REVIEW = "needs_human_review"
    JOB_STATUS_FAILED = "failed"
    JOB_STATUS_CANCELLED = "cancelled"
    BloggingError = Exception

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


class _QuietAccessFilter(logging.Filter):
    """Suppress noisy 200 OK access logs for health checks and polling endpoints.

    Only successful (200) requests to /health, /jobs, and /job/{id} are suppressed.
    Warnings, errors, and non-200 responses are always logged.
    """

    _QUIET_PATTERNS = ("/health", "/jobs", "/job/")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        msg = record.getMessage()
        if "200" in msg and any(p in msg for p in self._QUIET_PATTERNS):
            return False
        return True


logging.getLogger("uvicorn.access").addFilter(_QuietAccessFilter())

# Base directory for run artifacts (when work_dir is requested).
# Resolution order (persistent first — /tmp is a last-resort fallback that
# does NOT survive container restarts):
#   1. $BLOGGING_RUN_ARTIFACTS_ROOT (explicit override)
#   2. $AGENT_CACHE/blogging_team/runs (shared volume convention)
#   3. tempfile.gettempdir()/blogging_runs (ephemeral — logs a loud warning)
_custom_artifacts_root = os.environ.get("BLOGGING_RUN_ARTIFACTS_ROOT", "").strip()
_agent_cache_root = os.environ.get("AGENT_CACHE", "").strip()
if _custom_artifacts_root:  # pragma: no cover - selected at module import based on env; conftest sets AGENT_CACHE so this branch never runs in tests.
    RUN_ARTIFACTS_BASE = Path(_custom_artifacts_root).expanduser().resolve()
elif _agent_cache_root:  # pragma: no cover - selected at module import based on env; the tempdir fallback below is the path exercised in unit tests.
    RUN_ARTIFACTS_BASE = Path(_agent_cache_root).expanduser().resolve() / "blogging_team" / "runs"
else:
    RUN_ARTIFACTS_BASE = Path(tempfile.gettempdir()) / "blogging_runs"
    logger.warning(
        "Neither BLOGGING_RUN_ARTIFACTS_ROOT nor AGENT_CACHE is set — "
        "run artifacts will be written to %s, which is NOT persistent across "
        "process/container restarts. Set BLOGGING_RUN_ARTIFACTS_ROOT or AGENT_CACHE "
        "to a mounted volume for production deployments.",
        RUN_ARTIFACTS_BASE,
    )


def _run_blogging_service_shutdown() -> (
    None
):  # pragma: no cover - process-lifecycle shutdown hook driven by uvicorn; meaningful exercise needs a live server and real Temporal/job-service clients. All branches are defensive try/except around external subsystems.
    """Runs while Uvicorn still has the event loop; before process exit (replaces atexit hook)."""
    try:
        from agents.blogging.shared.blog_job_store import stop_blog_stale_monitor

        stop_blog_stale_monitor()
    except Exception:
        logger.debug("Stale job monitor stop skipped", exc_info=True)

    logger.info("Blogging service shutdown: notifying job-service…")
    try:
        from job_service_client import JobServiceClient

        client = JobServiceClient(team="blogging_team")
        client.mark_all_active_jobs_interrupted(
            "Blogging service shutting down",
            http_timeout=5.0,
            http_max_retries=0,
        )
    except Exception as exc:
        logger.info("Job-service shutdown notification skipped: %s", exc)

    logger.info("Blogging service shutdown: stopping Temporal worker…")
    try:
        from agents.blogging.temporal.worker import shutdown_blogging_temporal_components

        shutdown_blogging_temporal_components(worker_shutdown_timeout=8.0)
    except Exception:
        logger.warning("Temporal worker shutdown failed", exc_info=True)

    try:
        from agents.blogging.shared.job_event_bus import shutdown as _shutdown_event_bus

        _shutdown_event_bus()
    except Exception:
        logger.debug("Event-bus reaper shutdown skipped", exc_info=True)

    # The async-job workers are daemon threads (see _submit_async_job): the interpreter
    # reclaims them at exit without joining, so no explicit executor teardown is needed —
    # an in-flight HITL-parked job never blocks process shutdown. Any active jobs were
    # already marked interrupted above via mark_all_active_jobs_interrupted.


# Standard team wiring: init_otel + Postgres-schema lifespan + OTel instrument.
# The Postgres schema registers on startup (no-op when POSTGRES_HOST is unset);
# the on_shutdown hook runs the blogging service teardown (stale-monitor stop,
# job-service interrupt notification, Temporal worker shutdown) before the pool
# is closed.
app = create_team_app(
    service_name="blogging-team",
    team_key="blogging",
    title="Blog Research & Review API",
    description="Blog pipeline: planning, drafting, and quality gates. Supports sync and async execution with job polling and SSE.",
    version="0.3.0",
    postgres_schema=BLOGGING_POSTGRES_SCHEMA,
    on_shutdown=_run_blogging_service_shutdown,
)

# --- Public contract / re-exports (keep import + monkeypatch surface stable) ---
from agents.blogging.api import models as _api_models  # noqa: E402
from agents.blogging.api.background import (  # noqa: E402,F401
    _import_run_pipeline,
    _prepare_pipeline_input,
    _publish_skip_terminal_event,
    _publish_terminal_event,
    _run_pipeline_with_tracking,
)
from agents.blogging.api.job_workers import (  # noqa: E402,F401
    _ASYNC_JOB_MAX_WORKERS,
    _ASYNC_JOB_QUEUE,
    _ASYNC_JOB_WORKERS_LOCK,
    JobItem,
    _async_job_worker,
    _ensure_async_workers,
    _job_already_terminal,
    _submit_async_job,
)
from agents.blogging.api.models import (  # noqa: E402,F401
    ArtifactContentResponse,
    ArtifactListResponse,
    ArtifactMeta,
    AudienceDetails,
    BlogAnswersRequest,
    BlogJobListItem,
    BlogJobStatusResponse,
    CancelJobResponse,
    DeleteJobResponse,
    DraftFeedbackRequest,
    FullPipelineRequest,
    FullPipelineResponse,
    RateTitlesRequest,
    SelectTitleRequest,
    StartPipelineResponse,
    StoryResponseRequest,
    TitleChoiceResponse,
    TitleRatingItem,
    _blog_job_dict_to_status_response,
    _format_audience,
)


def _rebuild_api_models() -> None:
    """Resolve PEP 563 annotations for every Pydantic model defined in ``api.models``.

    ``api.models`` uses ``from __future__ import annotations``, so all annotations
    are strings at runtime; Pydantic must resolve them against that module's
    namespace before models with forward references can validate. We scan
    ``vars(_api_models)`` for locally-defined ``BaseModel`` subclasses so any model
    added to that file is rebuilt automatically — there is no hand-maintained list
    to fall out of sync (a new model that was forgotten would silently keep
    unresolved annotations).

    Preconditions:
        - Called after all model classes in ``api.models`` are defined (the module
          bottom, or from a test helper once the module has finished executing).
    Postconditions:
        - Every ``BaseModel`` subclass defined in ``api.models`` has its annotations
          resolved. ``model_rebuild`` is a no-op for already-complete models, so
          repeat calls are safe (idempotent).
    Invariants:
        - ``BaseModel`` subclasses imported from other modules are left untouched
          (filtered by ``__module__``), matching the resolution scope of the
          original hand-maintained list.
    """
    _ns: Dict[str, Any] = {**vars(_api_models)}
    for _obj in list(_ns.values()):
        if (
            isinstance(_obj, type)
            and issubclass(_obj, BaseModel)
            and _obj.__module__ == _api_models.__name__
        ):
            _obj.model_rebuild(_types_namespace=_ns)


_rebuild_api_models()

# Mount the concern-grouped routers last, so the route modules' late
# ``from …api import main as _main`` dereferences bind a fully-populated hub.
from agents.blogging.api.routers import (  # noqa: E402
    artifacts,
    interactive,
    jobs,
    medium_stats,
    pipeline,
    stories,
)

# Re-export a name tests call directly (not through an HTTP request), so it stays
# reachable as ``main._run_medium_stats_async_job`` after moving into its router.
_run_medium_stats_async_job = medium_stats._run_medium_stats_async_job  # noqa: F401

app.include_router(pipeline.router)
app.include_router(medium_stats.router)
app.include_router(jobs.router)
app.include_router(interactive.router)
app.include_router(artifacts.router)
app.include_router(stories.router)
