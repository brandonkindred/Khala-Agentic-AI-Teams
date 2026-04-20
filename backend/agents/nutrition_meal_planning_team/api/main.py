"""FastAPI server for Nutrition & Meal Planning team."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from shared_observability import init_otel, instrument_fastapi_app

from ..models import (
    BiometricHistoryResponse,
    BiometricPatchRequest,
    ChatRequest,
    ChatResponse,
    ClientProfile,
    ClinicalPatchRequest,
    ClinicianOverrideRequest,
    CompletenessResponse,
    FeedbackRequest,
    FeedbackResponse,
    MealHistoryResponse,
    MealPlanRequest,
    NutritionPlanRequest,
    ProfileUpdateRequest,
)
from ..orchestrator.agent import NutritionMealPlanningOrchestrator
from ..shared.job_store import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    create_job,
    get_job,
    is_job_cancelled,
    update_job,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

init_otel(service_name="nutrition-meal-planning-team", team_key="nutrition_meal_planning")


@asynccontextmanager
async def _nutrition_lifespan(app: FastAPI):
    # Register Postgres schema (no-op when POSTGRES_HOST is unset).
    try:
        from nutrition_meal_planning_team.postgres import SCHEMA as NUTRITION_POSTGRES_SCHEMA
        from shared_postgres import register_team_schemas

        register_team_schemas(NUTRITION_POSTGRES_SCHEMA)
    except Exception:
        logger.exception("nutrition_meal_planning postgres schema registration failed")
    yield
    try:
        from shared_postgres import close_pool

        close_pool()
    except Exception:
        logger.warning("nutrition_meal_planning shared_postgres close_pool failed", exc_info=True)


app = FastAPI(
    title="Nutrition & Meal Planning API",
    description="Personal nutrition and meal planning with learning from feedback",
    version="0.1.0",
    lifespan=_nutrition_lifespan,
)
instrument_fastapi_app(app, team_key="nutrition_meal_planning")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

orchestrator = NutritionMealPlanningOrchestrator()


@app.get("/health")
def health():
    """Health check for the Nutrition & Meal Planning team."""
    return {"status": "ok", "team": "nutrition_meal_planning"}


@app.get("/health/ready")
def health_ready():
    """Deep health check: verifies the Postgres connection is reachable."""
    checks: dict = {"team": "nutrition_meal_planning"}
    try:
        from shared_postgres import get_conn

        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        checks["postgres"] = "ok"
    except Exception as e:
        checks["postgres"] = f"error: {e}"

    all_ok = all(v == "ok" for k, v in checks.items() if k != "team")
    checks["status"] = "ok" if all_ok else "degraded"
    return checks


@app.post("/chat", response_model=ChatResponse)
def post_chat_route(body: ChatRequest):
    """Conversational chat endpoint.  Drives the full nutrition workflow through natural dialogue."""
    from ..shared.conversation_store import append_message

    client_id = body.client_id.strip()
    if not client_id:
        raise HTTPException(status_code=400, detail="client_id is required")

    # Persist the user message
    append_message(client_id, "user", body.message)

    response = orchestrator.handle_chat(body)

    # Persist the assistant response
    append_message(
        client_id,
        "assistant",
        response.message,
        phase=response.phase,
        action=response.action,
    )

    return response


@app.get(
    "/chat/history/{client_id}",
    summary="Get conversation history for a client",
    description="Returns the full persisted conversation history. Empty list if no history.",
)
def get_chat_history(client_id: str):
    """Retrieve persisted conversation history."""
    from ..shared.conversation_store import get_conversation

    return {"client_id": client_id, "messages": get_conversation(client_id.strip())}


@app.delete("/chat/history/{client_id}", summary="Clear conversation history")
def clear_chat_history(client_id: str):
    """Delete all conversation history for a client."""
    from ..shared.conversation_store import clear_conversation

    clear_conversation(client_id.strip())
    return {"client_id": client_id, "message": "Conversation history cleared."}


@app.get("/profile/{client_id}", response_model=ClientProfile)
def get_profile_route(client_id: str):
    """Get client profile. Returns 404 if not found."""
    profile = orchestrator.get_profile(client_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


@app.put("/profile/{client_id}", response_model=ClientProfile)
def put_profile_route(client_id: str, body: ProfileUpdateRequest):
    """Update client profile. Intake agent validates/completes; profile is saved."""
    return orchestrator.update_profile(client_id, body)


@app.patch("/profile/{client_id}/biometrics", response_model=ClientProfile)
def patch_biometrics_route(client_id: str, body: BiometricPatchRequest):
    """SPEC-002: append-only biometric update.

    Metric inputs (cm, kg) take precedence; imperial inputs are
    coerced to canonical units before validation. Every changed
    field writes a row to ``nutrition_biometric_log``.
    """
    return orchestrator.patch_biometrics(client_id, body)


@app.get(
    "/profile/{client_id}/biometrics/history",
    response_model=BiometricHistoryResponse,
)
def get_biometrics_history_route(
    client_id: str,
    field: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 200,
):
    """SPEC-002: biometric log time series.

    Optional ``field`` and ``since`` (ISO timestamp) narrow the query.
    """
    return orchestrator.get_biometric_history(client_id, field=field, since_iso=since, limit=limit)


@app.patch("/profile/{client_id}/clinical", response_model=ClientProfile)
def patch_clinical_route(client_id: str, body: ClinicalPatchRequest):
    """SPEC-002: update clinical info (conditions, meds, reproductive state, ED flag).

    Whole-list replace for ``conditions`` and ``medications``.
    Unrecognized strings land in the ``*_freetext`` lists on the
    clinical sub-object.
    """
    return orchestrator.patch_clinical(client_id, body)


@app.put("/profile/{client_id}/clinical-overrides", response_model=ClientProfile)
def put_clinician_overrides_route(client_id: str, body: ClinicianOverrideRequest):
    """SPEC-002: replace clinician-authored numeric overrides.

    Admin-only in production deployments. v1 enforces the admin
    boundary via the platform's security gateway; this route itself
    does not re-check auth.
    """
    return orchestrator.put_clinician_overrides(client_id, body)


@app.get("/profile/{client_id}/completeness", response_model=CompletenessResponse)
def get_profile_completeness_route(client_id: str):
    """SPEC-002: completeness blockers + minor / ED flags.

    Drives UI gating. Never 404s — an unknown client returns a
    response with ``no_profile`` in blockers.
    """
    return orchestrator.get_completeness(client_id)


def _run_nutrition_plan_job(job_id: str, body: NutritionPlanRequest) -> None:
    """Background: run nutrition plan, store result in job."""
    try:
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_RUNNING)
        result = orchestrator.get_nutrition_plan(body)
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_COMPLETED, result=result.model_dump())
    except ValueError as e:
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e), not_found=True)
    except Exception as e:
        logger.exception("Nutrition plan job %s failed", job_id)
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))


def _run_nutrition_regenerate_job(job_id: str, client_id: str) -> None:
    try:
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_RUNNING)
        result = orchestrator.regenerate_nutrition_plan(client_id)
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_COMPLETED, result=result.model_dump())
    except ValueError as e:
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e), not_found=True)
    except Exception as e:
        logger.exception("Nutrition regenerate job %s failed", job_id)
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))


def _run_meal_plan_job(job_id: str, body: MealPlanRequest) -> None:
    """Background: run meal plan, store result in job."""
    try:
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_RUNNING)
        result = orchestrator.get_meal_plan(body)
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_COMPLETED, result=result.model_dump())
    except ValueError as e:
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e), not_found=True)
    except Exception as e:
        logger.exception("Meal plan job %s failed", job_id)
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))


def _submit_thread_job(job_id: str, target, args) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


@app.post("/plan/nutrition")
def post_plan_nutrition_route(body: NutritionPlanRequest):
    """Submit an async nutrition plan job. Poll GET /jobs/{job_id} for the result."""
    job_id = str(uuid4())
    create_job(job_id, request=body.model_dump(), kind="nutrition_plan")
    _submit_thread_job(job_id, _run_nutrition_plan_job, (job_id, body))
    return {"job_id": job_id, "status": JOB_STATUS_PENDING}


@app.post("/plan/nutrition/{client_id}/regenerate")
def post_plan_nutrition_regenerate_route(client_id: str):
    """Submit an async regenerate job. Poll GET /jobs/{job_id} for the rebuilt plan.

    SPEC-004 §4.7: force cache miss and rebuild the plan. Rate-limited at
    the gateway layer in production.
    """
    job_id = str(uuid4())
    create_job(
        job_id,
        client_id=client_id,
        kind="nutrition_regenerate",
    )
    _submit_thread_job(job_id, _run_nutrition_regenerate_job, (job_id, client_id))
    return {"job_id": job_id, "status": JOB_STATUS_PENDING}


@app.get("/plan/nutrition/{client_id}/rationale")
def get_plan_nutrition_rationale_route(client_id: str):
    """SPEC-004 §4.7: rationale + intermediates for the latest plan.

    Lighter payload than the full plan (no guidelines / foods lists),
    designed for the "why these numbers?" UI panel. Returns 404 if
    no profile exists yet.
    """
    payload = orchestrator.get_rationale(client_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return payload


@app.post("/plan/meals")
def post_plan_meals_route(body: MealPlanRequest):
    """Submit an async meal plan job. Poll GET /jobs/{job_id} for the result."""
    job_id = str(uuid4())
    create_job(job_id, request=body.model_dump(), kind="meal_plan")
    try:
        from nutrition_meal_planning_team.temporal.client import is_temporal_enabled
        from nutrition_meal_planning_team.temporal.start_workflow import start_meal_plan_workflow

        if is_temporal_enabled():
            start_meal_plan_workflow(job_id, body.model_dump())
            return {"job_id": job_id, "status": JOB_STATUS_PENDING}
    except ImportError:
        pass
    _submit_thread_job(job_id, _run_meal_plan_job, (job_id, body))
    return {"job_id": job_id, "status": JOB_STATUS_PENDING}


@app.get("/jobs/{job_id}")
def get_job_route(job_id: str):
    """Get job status and result. Completed results live in the `result` field."""
    data = get_job(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return data


@app.post("/feedback", response_model=FeedbackResponse)
def post_feedback_route(body: FeedbackRequest):
    """Submit feedback for a recommendation (rating, would_make_again, notes)."""
    return orchestrator.submit_feedback(body)


@app.get("/history/meals", response_model=MealHistoryResponse)
def get_history_meals_route(client_id: Optional[str] = None):
    """Get past recommendations and feedback for the client."""
    if not client_id:
        raise HTTPException(status_code=400, detail="client_id required")
    return orchestrator.get_meal_history(client_id)
