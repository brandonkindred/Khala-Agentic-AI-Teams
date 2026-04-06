"""FastAPI server for Nutrition & Meal Planning team."""

from __future__ import annotations

import logging
import threading
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from ..models import (
    ChatRequest,
    ChatResponse,
    ClientProfile,
    FeedbackRequest,
    FeedbackResponse,
    MealHistoryResponse,
    MealPlanRequest,
    MealPlanResponse,
    NutritionPlanRequest,
    NutritionPlanResponse,
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
    update_job,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Nutrition & Meal Planning API",
    description="Personal nutrition and meal planning with learning from feedback",
    version="0.1.0",
)

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
    """Deep health check: verifies store directories are writable."""
    checks: dict = {"team": "nutrition_meal_planning"}
    try:
        test_dir = orchestrator.profile_store.storage_dir
        test_dir.mkdir(parents=True, exist_ok=True)
        checks["profile_store"] = "ok"
    except Exception as e:
        checks["profile_store"] = f"error: {e}"
    try:
        test_dir = orchestrator.meal_feedback_store.storage_dir
        test_dir.mkdir(parents=True, exist_ok=True)
        checks["meal_feedback_store"] = "ok"
    except Exception as e:
        checks["meal_feedback_store"] = f"error: {e}"
    try:
        test_dir = orchestrator.nutrition_plan_store.storage_dir
        test_dir.mkdir(parents=True, exist_ok=True)
        checks["nutrition_plan_store"] = "ok"
    except Exception as e:
        checks["nutrition_plan_store"] = f"error: {e}"

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
        client_id, "assistant", response.message,
        phase=response.phase, action=response.action,
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


@app.post("/plan/nutrition", response_model=NutritionPlanResponse)
def post_plan_nutrition_route(body: NutritionPlanRequest):
    """Get nutrition plan for client. Loads profile, runs nutritionist agent, returns plan."""
    try:
        return orchestrator.get_nutrition_plan(body)
    except ValueError:
        raise HTTPException(status_code=404, detail="Profile not found")


@app.post("/plan/meals", response_model=MealPlanResponse)
def post_plan_meals_route(body: MealPlanRequest):
    """Get meal plan: load profile, nutrition plan, meal history; run meal planning agent."""
    try:
        return orchestrator.get_meal_plan(body)
    except ValueError:
        raise HTTPException(status_code=404, detail="Profile not found")


def _run_meal_plan_job(job_id: str, body: MealPlanRequest) -> None:
    """Background: run meal plan, store result in job."""
    try:
        update_job(job_id, status=JOB_STATUS_RUNNING)
        result = orchestrator.get_meal_plan(body)
        update_job(job_id, status=JOB_STATUS_COMPLETED, result=result.model_dump())
    except ValueError as e:
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))
    except Exception as e:
        logger.exception("Meal plan job %s failed", job_id)
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))


@app.post("/plan/meals/async")
def post_plan_meals_async_route(body: MealPlanRequest):
    """Start async meal plan generation. Returns job_id; poll GET /jobs/{job_id} for result."""
    job_id = str(uuid4())
    create_job(job_id, status=JOB_STATUS_PENDING, request=body.model_dump())
    try:
        from nutrition_meal_planning_team.temporal.client import is_temporal_enabled
        from nutrition_meal_planning_team.temporal.start_workflow import start_meal_plan_workflow

        if is_temporal_enabled():
            start_meal_plan_workflow(job_id, body.model_dump())
            return {"job_id": job_id}
    except ImportError:
        pass
    thread = threading.Thread(target=_run_meal_plan_job, args=(job_id, body), daemon=True)
    thread.start()
    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def get_job_route(job_id: str):
    """Get job status and result (for async meal plan). Result in payload when status is completed."""
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
