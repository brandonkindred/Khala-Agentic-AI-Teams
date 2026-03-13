"""FastAPI server for Nutrition & Meal Planning team."""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ..models import (
    ClientProfile,
    FeedbackRequest,
    FeedbackResponse,
    MealHistoryResponse,
    MealPlanRequest,
    MealPlanResponse,
    MealRecommendationWithId,
    NutritionPlanRequest,
    NutritionPlanResponse,
    ProfileUpdateRequest,
)
from ..shared.client_profile_store import ClientProfileStore
from ..shared.job_store import (
    create_job,
    get_job,
    update_job,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
)
from ..shared.meal_feedback_store import MealFeedbackStore
from llm_service import get_client
from ..agents.intake_profile_agent import IntakeProfileAgent
from ..agents.meal_planning_agent import MealPlanningAgent
from ..agents.nutritionist_agent import NutritionistAgent

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

profile_store = ClientProfileStore()
meal_feedback_store = MealFeedbackStore()
llm = get_client("nutrition_meal_planning")
intake_agent = IntakeProfileAgent(llm)
nutritionist_agent = NutritionistAgent(llm)
meal_planning_agent = MealPlanningAgent(llm)


@app.get("/health")
async def health():
    """Health check for the Nutrition & Meal Planning team."""
    return {"status": "ok", "team": "nutrition_meal_planning"}


@app.get("/profile/{client_id}", response_model=ClientProfile)
async def get_profile_route(client_id: str):
    """Get client profile. Returns 404 if not found."""
    profile = profile_store.get_profile(client_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


@app.put("/profile/{client_id}", response_model=ClientProfile)
async def put_profile_route(client_id: str, body: ProfileUpdateRequest):
    """Update client profile. Intake agent validates/completes; profile is saved."""
    current = profile_store.get_profile(client_id)
    if current is None:
        current = profile_store.create_profile(client_id)
    profile = intake_agent.run(client_id, update=body, current_profile=current)
    profile_store.save_profile(client_id, profile)
    return profile


@app.post("/plan/nutrition", response_model=NutritionPlanResponse)
async def post_plan_nutrition_route(body: NutritionPlanRequest):
    """Get nutrition plan for client. Loads profile, runs nutritionist agent, returns plan."""
    profile = profile_store.get_profile(body.client_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    plan = nutritionist_agent.run(profile)
    return NutritionPlanResponse(client_id=body.client_id, plan=plan)


@app.post("/plan/meals", response_model=MealPlanResponse)
async def post_plan_meals_route(body: MealPlanRequest):
    """Get meal plan: load profile, nutrition plan, meal history; run meal planning agent; record each suggestion and return with recommendation_ids."""
    profile = profile_store.get_profile(body.client_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    nutrition_plan = nutritionist_agent.run(profile)
    meal_history = meal_feedback_store.get_meal_history(
        body.client_id, limit=50
    )
    suggestions = meal_planning_agent.run(
        profile,
        nutrition_plan,
        meal_history,
        period_days=body.period_days,
        meal_types=body.meal_types,
    )
    with_ids: list[MealRecommendationWithId] = []
    for s in suggestions:
        rec_id = meal_feedback_store.record_recommendation(
            body.client_id, s.model_dump()
        )
        with_ids.append(
            MealRecommendationWithId(
                **s.model_dump(),
                recommendation_id=rec_id,
            )
        )
    return MealPlanResponse(client_id=body.client_id, suggestions=with_ids)


def _run_meal_plan_job(job_id: str, body: MealPlanRequest) -> None:
    """Background: run meal plan, store result in job."""
    try:
        update_job(job_id, status=JOB_STATUS_RUNNING)
        profile = profile_store.get_profile(body.client_id)
        if profile is None:
            update_job(job_id, status=JOB_STATUS_FAILED, error="Profile not found")
            return
        nutrition_plan = nutritionist_agent.run(profile)
        meal_history = meal_feedback_store.get_meal_history(body.client_id, limit=50)
        suggestions = meal_planning_agent.run(
            profile, nutrition_plan, meal_history,
            period_days=body.period_days, meal_types=body.meal_types,
        )
        with_ids = []
        for s in suggestions:
            rec_id = meal_feedback_store.record_recommendation(body.client_id, s.model_dump())
            with_ids.append({**s.model_dump(), "recommendation_id": rec_id})
        result = MealPlanResponse(client_id=body.client_id, suggestions=with_ids)
        update_job(job_id, status=JOB_STATUS_COMPLETED, result=result.model_dump())
    except Exception as e:
        logger.exception("Meal plan job %s failed", job_id)
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))


@app.post("/plan/meals/async")
async def post_plan_meals_async_route(body: MealPlanRequest):
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
async def get_job_route(job_id: str):
    """Get job status and result (for async meal plan). Result in payload when status is completed."""
    data = get_job(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return data


@app.post("/feedback", response_model=FeedbackResponse)
async def post_feedback_route(body: FeedbackRequest):
    """Submit feedback for a recommendation (rating, would_make_again, notes)."""
    ok = meal_feedback_store.record_feedback(
        body.recommendation_id,
        rating=body.rating,
        would_make_again=body.would_make_again,
        notes=body.notes,
    )
    return FeedbackResponse(recommendation_id=body.recommendation_id, recorded=ok)


@app.get("/history/meals", response_model=MealHistoryResponse)
async def get_history_meals_route(client_id: Optional[str] = None):
    """Get past recommendations and feedback for the client."""
    if not client_id:
        raise HTTPException(status_code=400, detail="client_id required")
    entries = meal_feedback_store.get_meal_history(client_id, limit=100)
    return MealHistoryResponse(client_id=client_id, entries=entries)
