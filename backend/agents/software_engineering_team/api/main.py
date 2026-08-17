"""FastAPI application for the software engineering team.

Async API: POST /run-team returns job_id, GET /run-team/{job_id} polls status.
Tech Lead orchestrator runs in background.

This module is the thin app-assembly hub. Responsibility-focused sub-modules hold
the actual logic:

* ``_paths`` — sys.path bootstrap (run first via ``api/__init__``).
* ``models`` — Pydantic request/response schemas.
* ``state`` — shared mutable globals + pure parse/validation helpers.
* ``lifecycle`` — ASGI startup/shutdown hooks.
* ``api.routes.*`` — SE's own APIRouter modules, grouped by concern.
* ``api.routes.coding_team_*`` / ``api.routes.github`` / ``api.routes.reviews``
  — the coding-team execution engine's own APIRouter modules, mounted
  unprefixed onto this same app so ``/api/coding-team/*`` keeps resolving
  unchanged; see the mounting block below for why ``api.coding_team_main`` is
  imported first.

Every moved public symbol is re-imported here so ``from …api.main import X`` and
``monkeypatch.setattr(main, "X", …)`` keep working unchanged: this module remains
the single owning namespace for the monkeypatched collaborators, which the route
modules dereference at call time via ``main``.
"""

import logging
import threading  # noqa: F401  (public re-export: tests patch main.threading.Thread)

from fastapi.middleware.cors import CORSMiddleware

from shared.app import create_team_app

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
from software_engineering_team.spec_parser import SPEC_FILENAME  # noqa: F401

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
# The coding-team execution engine's own routers, mounted unprefixed onto this
# same app so /api/coding-team/* (proxied in by unified_api, see
# unified_api/config.py) keeps resolving to identical paths now that the
# standalone coding-team service is retired. `coding_team_hitl`/`coding_team_jobs`
# are the on-disk module names (they collided with SE's own `hitl`/`jobs` route
# modules and were renamed on merge); `jobs` there omits `/health` (SE's
# `status.router` already serves it) so there is no route-path collision.
#
# Import the coding-team engine's OWN api.coding_team_main hub FIRST (unused
# directly, hence the F401 suppression below): its route modules do `from
# …api import coding_team_main as _main`, a circular import that only resolves
# cleanly when `api.coding_team_main` is already mid-import (as it is when IT
# is the entry point). Importing the route modules here without going through
# that hub first makes THIS import the entry point instead, which trips the
# same cycle from the opposite, unresolved direction (AttributeError: partially
# initialized module).
from software_engineering_team.api import coding_team_main as _coding_team_main  # noqa: E402,F401
from software_engineering_team.api.routes import (  # noqa: E402
    architect,
    code_v2,
    coding_team_hitl,
    coding_team_jobs,
    execution,
    hitl,
    jobs,
    product_analysis,
    status,
)
from software_engineering_team.api.routes import (  # noqa: E402
    github as coding_team_github,
)
from software_engineering_team.api.routes import (  # noqa: E402
    issue_grooming as coding_team_issue_grooming,
)
from software_engineering_team.api.routes import (  # noqa: E402
    reviews as coding_team_reviews,
)
from software_engineering_team.api.routes._common import (  # noqa: E402
    register_job_service_unavailable_handlers,
)

# Coding-team routes (and SE's own job-store routes) call JobServiceClient;
# map exhausted transport errors to 503 on this process's app (production entry).
register_job_service_unavailable_handlers(app)

app.include_router(jobs.router)
app.include_router(hitl.router)
app.include_router(execution.router)
app.include_router(architect.router)
app.include_router(code_v2.router)
app.include_router(product_analysis.router)
app.include_router(status.router)
app.include_router(coding_team_jobs.router)
app.include_router(coding_team_hitl.router)
app.include_router(coding_team_github.router)
app.include_router(coding_team_issue_grooming.router)
app.include_router(coding_team_reviews.router)
