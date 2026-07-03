"""FastAPI application for the software engineering team.

Async API: POST /run-team returns job_id, GET /run-team/{job_id} polls status.
Tech Lead orchestrator runs in background.

This module is the thin app-assembly hub. Responsibility-focused sub-modules hold
the actual logic:

* ``_paths`` — sys.path bootstrap (run first via ``api/__init__``).
* ``models`` — Pydantic request/response schemas.
* ``state`` — shared mutable globals + pure parse/validation helpers.
* ``lifecycle`` — ASGI startup/shutdown hooks.
* ``background`` — orchestrator/runner thread targets.
* ``routes/*`` — APIRouter modules grouped by concern.

Every moved public symbol is re-imported here so ``from …api.main import X`` and
``monkeypatch.setattr(main, "X", …)`` keep working unchanged: this module remains
the single owning namespace for the monkeypatched collaborators, which the route
and background modules dereference at call time via ``main``.
"""

import logging
import threading  # noqa: F401  (public re-export: tests patch main.threading.Thread)

from fastapi.middleware.cors import CORSMiddleware
from spec_parser import (  # noqa: F401
    SPEC_FILENAME,
    validate_work_path,
    validate_workspace_path_no_spec,
)

from shared_app import create_team_app
from software_engineering_team.api.background import (  # noqa: F401
    _run_backend_code_v2_background,
    _run_frontend_code_v2_background,
    _run_orchestrator_background,
    _run_product_analysis_background,
    _run_retry_background,
)

# --- Public contract / re-exports (keep import + monkeypatch surface stable) ---
from software_engineering_team.api.lifecycle import (  # noqa: F401
    _se_shutdown,
    _se_startup,
)
from software_engineering_team.api.models import (  # noqa: F401
    AnswerSubmission,
    ArchitectDesignRequest,
    ArchitectDesignResponse,
    AutoAnswerRequest,
    AutoAnswerResponse,
    BackendCodeV2MicrotaskStatus,
    BackendCodeV2RunRequest,
    BackendCodeV2RunResponse,
    BackendCodeV2StatusResponse,
    BackendCodeV2TaskInput,
    CancelJobResponse,
    CurrentActivityEntry,
    DeleteJobResponse,
    FailedTaskDetail,
    FrontendCodeV2RunRequest,
    FrontendCodeV2RunResponse,
    FrontendCodeV2StatusResponse,
    FrontendCodeV2TaskInput,
    JobStatusResponse,
    PendingQuestion,
    PlanningArtifactContentResponse,
    PlanningArtifactListResponse,
    PlanningArtifactMeta,
    PlanningV2ResultResponse,
    PlanningV2RunRequest,
    PlanningV2RunResponse,
    PlanningV2StatusResponse,
    ProductAnalysisRunRequest,
    ProductAnalysisRunResponse,
    ProductAnalysisStatusResponse,
    QuestionOption,
    RetryResponse,
    RunningJobsResponse,
    RunningJobSummary,
    RunTeamRequest,
    RunTeamResponse,
    StartFromSpecRequest,
    SubmitAnswersRequest,
    TaskStateEntry,
    TeamProgressEntry,
)
from software_engineering_team.api.state import (  # noqa: F401
    ALLOWED_SERVICES,
    DEFAULT_PROJECTS_DIR_NAME,
    ENV_WORKSPACE_ROOT,
    PROJECT_NAME_PATTERN,
    RESTARTABLE_STATUSES,
    RESUMABLE_STATUSES,
    SUPERVISOR_LOG_DIR,
    _active_orchestrator_threads,
    _coerce_current_activity,
    _coerce_progress,
    _get_projects_root,
    _get_spec_content_for_job,
    _get_workspace_base_dir,
    _is_orchestrator_alive,
    _parse_task_states,
    _parse_team_progress,
    _preflight_sprint_scope,
    _real_question_options,
    _start_stale_job_monitor_once,
    create_project_workspace,
)
from software_engineering_team.postgres import SCHEMA as SE_POSTGRES_SCHEMA
from software_engineering_team.shared.execution_tracker import execution_tracker  # noqa: F401
from software_engineering_team.shared.job_store import (  # noqa: F401
    JOB_STATUS_AGENT_CRASH,
    JOB_STATUS_ALREADY_COMPLETE,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PAUSED_LLM_CONNECTIVITY,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    create_job,
    delete_job,
    get_job,
    update_job,
)
from software_engineering_team.shared.logging_config import setup_logging

setup_logging(level=logging.INFO)
logger = logging.getLogger(__name__)

# Standard team wiring: init_otel + Postgres-schema lifespan + OTel instrument.
# Telemetry-observer registration and Temporal worker start run as the startup
# hook (after schema registration); marking active jobs failed runs as the
# shutdown hook (before the pool is closed).
app = create_team_app(
    service_name="software-engineering-team",
    team_key="software_engineering",
    title="Software Engineering Team API",
    description="Async API: POST /run-team with work folder path returns job_id. "
    "GET /run-team/{job_id} polls status. Tech Lead orchestrates the full pipeline.",
    version="0.3.0",
    postgres_schema=SE_POSTGRES_SCHEMA,
    on_startup=_se_startup,
    on_shutdown=_se_shutdown,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount the concern-grouped routers. Imported last so the route modules'
# ``from …api import main as _main`` binds a fully-populated hub (app + all
# re-exported collaborators already defined above).
from software_engineering_team.api.routes import (  # noqa: E402
    architect,
    code_v2,
    execution,
    hitl,
    jobs,
    product_analysis,
    status,
)

app.include_router(jobs.router)
app.include_router(hitl.router)
app.include_router(execution.router)
app.include_router(architect.router)
app.include_router(code_v2.router)
app.include_router(product_analysis.router)
app.include_router(status.router)
