"""FastAPI server for Nutrition & Meal Planning team."""

from __future__ import annotations

import logging
import threading
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from llm_service import get_client

from ..agents.chat_agent import NutritionChatAgent
from ..agents.intake_profile_agent import IntakeProfileAgent
from ..agents.meal_planning_agent import MealPlanningAgent
from ..agents.nutritionist_agent import NutritionistAgent
from ..models import (
    ChatRequest,
    ChatResponse,
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
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    create_job,
    get_job,
    update_job,
)
from ..shared.meal_feedback_store import MealFeedbackStore

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
chat_agent = NutritionChatAgent(llm, intake_agent, nutritionist_agent, meal_planning_agent)


@app.get("/health")
async def health():
    """Health check for the Nutrition & Meal Planning team."""
    return {"status": "ok", "team": "nutrition_meal_planning"}


@app.post("/chat", response_model=ChatResponse)
async def post_chat_route(body: ChatRequest):
    """Conversational chat endpoint.  Drives the full nutrition workflow through natural dialogue."""
    client_id = body.client_id.strip()
    if not client_id:
        raise HTTPException(status_code=400, detail="client_id is required")

    # Load current state
    profile = profile_store.get_profile(client_id)
    nutrition_plan = None
    meal_suggestions_with_ids: list[MealRecommendationWithId] = []
    meal_history = []

    if profile is not None:
        try:
            nutrition_plan = nutritionist_agent.run(profile)
        except Exception:
            nutrition_plan = None
        meal_history = meal_feedback_store.get_meal_history(client_id, limit=50)

    history_dicts = [{"role": m.role, "content": m.content} for m in body.conversation_history]

    # Run the chat agent
    result = chat_agent.run(
        client_id=client_id,
        user_message=body.message,
        conversation_history=history_dicts,
        profile=profile,
        nutrition_plan=nutrition_plan,
        meal_suggestions=meal_suggestions_with_ids or None,
        meal_history=meal_history or None,
    )

    action = result.get("action", "none")
    response = ChatResponse(
        message=result.get("message", ""),
        phase=result.get("phase", "intake"),
        action=action,
    )

    # --- Handle actions ---

    if action == "save_profile":
        extracted = result.get("extracted_profile") or {}
        # Build a ProfileUpdateRequest from extracted data
        update_data: dict = {}
        if "household" in extracted:
            update_data["household"] = extracted["household"]
        if "dietary_needs" in extracted:
            update_data["dietary_needs"] = extracted["dietary_needs"]
        if "allergies_and_intolerances" in extracted:
            update_data["allergies_and_intolerances"] = extracted["allergies_and_intolerances"]
        if "lifestyle" in extracted:
            update_data["lifestyle"] = extracted["lifestyle"]
        if "preferences" in extracted:
            update_data["preferences"] = extracted["preferences"]
        if "goals" in extracted:
            update_data["goals"] = extracted["goals"]

        if update_data:
            update_req = ProfileUpdateRequest.model_validate(update_data)
            current = profile_store.get_profile(client_id)
            if current is None:
                current = profile_store.create_profile(client_id)
            saved_profile = intake_agent.run(client_id, update=update_req, current_profile=current)
            profile_store.save_profile(client_id, saved_profile)
            response.profile = saved_profile
        elif profile:
            response.profile = profile

    elif action == "generate_nutrition_plan":
        p = profile or profile_store.get_profile(client_id)
        if p:
            try:
                plan = nutritionist_agent.run(p)
                response.nutrition_plan = plan
            except Exception as e:
                logger.warning("Nutrition plan generation failed during chat: %s", e)

    elif action == "generate_meals":
        p = profile or profile_store.get_profile(client_id)
        if p:
            try:
                params = result.get("meal_plan_params") or {}
                period_days = params.get("period_days", 7)
                meal_types = params.get("meal_types", ["lunch", "dinner"])
                np = nutritionist_agent.run(p)
                mh = meal_feedback_store.get_meal_history(client_id, limit=50)
                suggestions = meal_planning_agent.run(
                    p, np, mh, period_days=period_days, meal_types=meal_types
                )
                with_ids: list[MealRecommendationWithId] = []
                for s in suggestions:
                    rec_id = meal_feedback_store.record_recommendation(client_id, s.model_dump())
                    with_ids.append(
                        MealRecommendationWithId(**s.model_dump(), recommendation_id=rec_id)
                    )
                response.meal_suggestions = with_ids
            except Exception as e:
                logger.warning("Meal plan generation failed during chat: %s", e)

    elif action == "submit_feedback":
        fb = result.get("feedback_data") or {}
        meal_name = fb.get("meal_name", "").strip().lower()
        rating = fb.get("rating")
        would_make_again = fb.get("would_make_again")
        notes = fb.get("notes", "")

        # Try to find the recommendation by name in recent history
        if meal_name and meal_history:
            for entry in meal_history:
                snap = entry.meal_snapshot or {}
                name = (snap.get("name") or "").strip().lower()
                if name and meal_name in name or name in meal_name:
                    meal_feedback_store.record_feedback(
                        entry.recommendation_id,
                        rating=rating,
                        would_make_again=would_make_again,
                        notes=notes,
                    )
                    response.feedback_recorded = True
                    break

    return response


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
    meal_history = meal_feedback_store.get_meal_history(body.client_id, limit=50)
    suggestions = meal_planning_agent.run(
        profile,
        nutrition_plan,
        meal_history,
        period_days=body.period_days,
        meal_types=body.meal_types,
    )
    with_ids: list[MealRecommendationWithId] = []
    for s in suggestions:
        rec_id = meal_feedback_store.record_recommendation(body.client_id, s.model_dump())
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
            profile,
            nutrition_plan,
            meal_history,
            period_days=body.period_days,
            meal_types=body.meal_types,
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
