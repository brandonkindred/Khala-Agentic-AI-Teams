"""FastAPI application for the branding strategy team.

This module is the thin app-assembly hub. Responsibility-focused sub-modules hold
the actual logic:

* ``models`` — Pydantic request/response schemas.
* ``state`` — the interactive-review session store + pure mission/question helpers.
* ``lifecycle`` — the ASGI shutdown hook.
* ``background`` — the run/job executor + Temporal-dispatch machinery.
* ``conversation`` — the chat-endpoint bodies and their helpers.
* ``api.routes.*`` — one ``APIRouter`` per concern, mounted below.

This module remains the single owning namespace for the collaborators the test
suite monkeypatches (``orchestrator``, ``assistant_agent``, ``branding_store``,
``_run_executor``, ``_job_manager``, ``_stale_monitor_stop``,
``_job_heartbeat_interval_s``) and re-exports the moved helpers, session store,
and request/response DTOs, so ``from …api.main import X`` and
``monkeypatch.setattr(main, "X", …)`` keep working unchanged for those names; the
route, background, and conversation modules dereference the collaborators through
``main`` at call time.

The route *handler* functions are the exception: they live on their ``APIRouter``
in ``api.routes.*`` (reached via the mounted app, never imported by name) and are
not re-bound here — matching the split-router convention in
``software_engineering_team/api``.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import threading
from typing import Any, ContextManager, Optional

from fastapi import HTTPException

# --- Public contract / re-exports (keep import + monkeypatch surface stable) ---
from branding_team.api.lifecycle import _branding_service_shutdown
from branding_team.api.models import (  # noqa: F401
    AnswerBrandingQuestionRequest,
    AttachConversationBrandRequest,
    BrandingQuestion,
    BrandingSession,
    BrandingSessionResponse,
    BrandJobListItem,
    BrandJobListResponse,
    BrandJobStatusResponse,
    ConversationMessage,
    ConversationStateResponse,
    ConversationSummaryResponse,
    CreateBrandRequest,
    CreateClientRequest,
    CreateConversationRequest,
    RunBrandingTeamRequest,
    RunBrandJobResponse,
    RunBrandRequest,
    SendMessageRequest,
    UpdateBrandRequest,
)
from branding_team.assistant import get_conversation_store
from branding_team.assistant.agent import BrandingAssistantAgent
from branding_team.orchestrator import (
    orchestrator,  # noqa: F401  (re-export: patched via main.orchestrator)
)
from branding_team.postgres import SCHEMA as BRANDING_POSTGRES_SCHEMA
from branding_team.shared.job_store import (  # noqa: F401
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
)
from branding_team.store import get_default_store
from job_service_client import JobServiceClient, start_stale_job_monitor
from shared_app import create_team_app
from shared_concurrency import BackgroundHeartbeat
from shared_env_config import env_float, env_int

logger = logging.getLogger(__name__)


def _max_concurrent_runs() -> int:
    """Worker cap for the branding-run executor (env-tunable, clamped to >= 1)."""
    return env_int("BRANDING_MAX_CONCURRENT_RUNS", 4, floor=1)


# Branding runs are submitted to a bounded pool instead of spawning an
# unbounded daemon thread per request. The fixed worker count gives the
# pipeline backpressure (extra submissions queue rather than fan out into
# thousands of concurrent LLM pipelines) while the job row stays PENDING
# until a worker picks it up.
_run_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_max_concurrent_runs(),
    thread_name_prefix="branding-run",
)


def _job_heartbeat_interval_s() -> float:
    """Heartbeat cadence for a running branding job (env-tunable, clamped to >= 1.0s)."""
    return env_float("BRANDING_JOB_HEARTBEAT_INTERVAL_S", 30.0, floor=1.0)


# Periodic sweep that fails jobs whose heartbeat has gone stale (e.g. a worker
# crashed mid-run). Degrades gracefully: a job-service outage at import time
# leaves both globals None instead of crashing the whole app.
#
# A branding pipeline can run for several minutes, and its bounded executor can
# leave extra submissions queued as PENDING. While a job is RUNNING its heartbeat
# is kept fresh by ``_job_heartbeat`` (see ``background._run_branding_core``), so
# the sweep never fails a live run regardless of length. The 900s window only has
# to cover the worst-case PENDING queue wait before a worker picks the job up — a
# job stuck PENDING that long is genuinely wedged and should be swept.
try:
    _job_manager = JobServiceClient(team="branding_team")
    _stale_monitor_stop: Optional[threading.Event] = start_stale_job_monitor(
        _job_manager,
        interval_seconds=15.0,
        stale_after_seconds=900.0,
        reason="Job heartbeat stale while pending/running",
    )
except Exception as _init_err:
    logger.warning("Branding job manager init failed: %s", _init_err)
    _job_manager = None
    _stale_monitor_stop = None


def _job_heartbeat(job_id: str) -> ContextManager[Any]:
    """Keep ``job_id``'s job-service heartbeat fresh while the pipeline runs.

    Preconditions:
        ``job_id`` refers to a job already created in the job store.
    Postconditions:
        Returns a context manager that, while active, pings the job service every
        ``_job_heartbeat_interval_s()`` seconds so the stale-job monitor never marks
        a valid long-running branding run as failed. A no-op context when the job
        manager is unavailable; a beat error is logged and never interrupts the run.
    """
    if _job_manager is None:
        return contextlib.nullcontext()
    return BackgroundHeartbeat(
        lambda: _job_manager.heartbeat(job_id),
        _job_heartbeat_interval_s(),
        name=f"branding-job-heartbeat-{job_id}",
        on_error=lambda exc: logger.warning("branding job %s heartbeat error: %s", job_id, exc),
    )


app = create_team_app(
    service_name="branding-team",
    team_key="branding",
    title="Branding Team API",
    version="2.0.0",
    postgres_schema=BRANDING_POSTGRES_SCHEMA,
    on_shutdown=_branding_service_shutdown,
)

branding_store = get_default_store()
conversation_store = get_conversation_store()

# Public name so tests can patch 'branding_team.api.main.assistant_agent'.
assistant_agent: Optional[BrandingAssistantAgent] = None
_assistant_agent_lock = threading.Lock()


def _get_assistant_agent() -> BrandingAssistantAgent:
    """Lazy-init the branding assistant so the app mounts even if llm_service is unavailable.

    Thread-safe: the chat endpoints run in worker threads (via
    ``background._run_in_pipeline_executor``), so first-use initialization is
    guarded by a ``threading.Lock`` with double-checked locking to avoid
    constructing several ``BrandingAssistantAgent`` instances under concurrent
    first requests.
    """
    global assistant_agent
    if assistant_agent is None:
        with _assistant_agent_lock:
            if assistant_agent is None:
                try:
                    assistant_agent = BrandingAssistantAgent()
                except Exception:
                    raise HTTPException(
                        status_code=503,
                        detail="Branding assistant is temporarily unavailable. LLM service may not be configured.",
                    )
    return assistant_agent


# --- Re-export the moved helpers (import + monkeypatch surface). Imported after
# the globals + app above so each module's ``from …api import main as _main``
# binds a fully-populated hub. ---
from branding_team.api.background import (  # noqa: E402,F401
    _run_branding_background,
    _run_branding_core,
    _run_in_pipeline_executor,
    _signal_branding_cancel,
    _submit_brand_run,
)
from branding_team.api.conversation import (  # noqa: E402,F401
    _auto_create_brand_from_conversation,
    _brand_exists,
    _conversation_to_response,
    _create_branding_conversation_impl,
    _ensure_default_client,
    _local_message,
    _run_orchestrator_if_ready,
    _send_branding_conversation_message_impl,
)

# Mount the concern-grouped routers last, so the route modules'
# ``from …api import main as _main`` binds a fully-populated hub.
from branding_team.api.routes import (  # noqa: E402
    brands,
    clients,
    conversations,
    health,
    integrations,
    runs,
    sessions,
)
from branding_team.api.state import (  # noqa: E402,F401
    _MISSION_PLACEHOLDERS,
    BrandingSessionStore,
    _apply_answer,
    _build_open_questions,
    _is_real_value,
    _mission_from_payload,
    _mission_has_brand_name,
    _mission_has_minimal_required_fields,
    _parse_target_phase,
    _session_response,
    session_store,
)

app.include_router(clients.router)
app.include_router(brands.router)
app.include_router(runs.router)
app.include_router(integrations.router)
app.include_router(sessions.router)
app.include_router(conversations.router)
app.include_router(health.router)
