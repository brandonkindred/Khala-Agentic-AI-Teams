"""FastAPI server for Personal Assistant team."""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..models import (
    AssistantRequest,
    AssistantResponse,
    CalendarEvent,
    Deal,
    EmailDraft,
    Reservation,
    TaskItem,
    TaskList,
    UserProfile,
)
from ..orchestrator.agent import PersonalAssistantOrchestrator
from ..shared.credential_store import CredentialStore
from ..shared.llm import get_llm_client
from ..shared.user_profile_store import UserProfileStore
from ..shared.pa_job_store import (
    cancel_job,
    create_job,
    delete_job,
    get_job,
    is_job_cancelled,
    list_jobs,
    update_job,
    PA_JOB_STATUS_CANCELLED,
    PA_JOB_STATUS_COMPLETED,
    PA_JOB_STATUS_FAILED,
    PA_JOB_STATUS_PENDING,
    PA_JOB_STATUS_RUNNING,
)

try:
    from unified_api.slack_notifier import notify_pa_response as slack_notify_pa_response
except ImportError:
    slack_notify_pa_response = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Personal Assistant API",
    description="A comprehensive personal assistant that manages email, calendars, tasks, deals, and more.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    """Serve the Personal Assistant UI."""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return HTMLResponse(
        content="""
        <html>
            <head><title>Personal Assistant</title></head>
            <body style="font-family: sans-serif; padding: 40px; text-align: center;">
                <h1>Personal Assistant API</h1>
                <p>The UI is not available. Please check that static/index.html exists.</p>
                <p><a href="/docs">View API Documentation</a></p>
            </body>
        </html>
        """,
        status_code=200
    )


llm = get_llm_client("personal_assistant")
credential_store = CredentialStore()
profile_store = UserProfileStore()
orchestrator = PersonalAssistantOrchestrator(llm, credential_store, profile_store)


class AssistantRequestBody(BaseModel):
    """Request body for assistant endpoint."""

    message: str
    context: Dict[str, Any] = Field(default_factory=dict)


class ProfileUpdateBody(BaseModel):
    """Request body for profile updates."""

    category: str
    data: Dict[str, Any]
    merge: bool = True


class EmailConnectBody(BaseModel):
    """Request body for email connection."""

    provider: str
    credentials: Dict[str, Any]


class EmailDraftBody(BaseModel):
    """Request body for email drafting."""

    intent: str
    context: Dict[str, Any] = Field(default_factory=dict)


class TaskListCreateBody(BaseModel):
    """Request body for creating a task list."""

    name: str
    description: str = ""


class TaskItemAddBody(BaseModel):
    """Request body for adding a task item."""

    description: str
    quantity: Optional[str] = None
    priority: str = "medium"
    due_date: Optional[str] = None
    notes: Optional[str] = None


class TasksFromTextBody(BaseModel):
    """Request body for adding tasks from text."""

    text: str


class CalendarEventCreateBody(BaseModel):
    """Request body for creating a calendar event."""

    title: str
    start_time: str
    end_time: Optional[str] = None
    duration_minutes: int = 60
    location: Optional[str] = None
    description: str = ""
    attendees: List[str] = Field(default_factory=list)


class EventFromTextBody(BaseModel):
    """Request body for creating event from text."""

    text: str
    auto_create: bool = False


class DealSearchBody(BaseModel):
    """Request body for deal search."""

    query: Optional[str] = None
    category: Optional[str] = None
    max_results: int = 10


class WishlistAddBody(BaseModel):
    """Request body for adding wishlist item."""

    description: str
    target_price: Optional[float] = None
    category: str = ""


class ReservationCreateBody(BaseModel):
    """Request body for creating a reservation."""

    reservation_type: str
    venue_name: Optional[str] = None
    datetime: str
    party_size: int = 1
    notes: str = ""


class ReservationFromTextBody(BaseModel):
    """Request body for creating reservation from text."""

    text: str


class DocumentGenerateBody(BaseModel):
    """Request body for document generation."""

    doc_type: str
    topic: str
    context: Dict[str, Any] = Field(default_factory=dict)


class ChecklistGenerateBody(BaseModel):
    """Request body for checklist generation."""

    task: str
    include_time_estimates: bool = False


class AssistantJobRequest(BaseModel):
    """Request body for starting an async assistant job."""

    message: str
    context: Dict[str, Any] = Field(default_factory=dict)
    async_mode: bool = Field(
        default=True,
        description="If True, run in background and return job_id. If False, run synchronously.",
    )


class AssistantJobResponse(BaseModel):
    """Response for async job submission."""

    job_id: str
    status: str = PA_JOB_STATUS_RUNNING
    message: str = "Request submitted. Poll GET /assistant/jobs/{job_id} for status."


class AssistantJobStatus(BaseModel):
    """Status response for a running or completed job."""

    job_id: str
    user_id: str
    status: str = Field(description="pending, running, completed, failed, cancelled")
    request_type: Optional[str] = Field(None, description="Classified intent type")
    progress: int = Field(0, ge=0, le=100)
    status_text: Optional[str] = Field(None, description="Human-readable status message")
    request_message: str
    response: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: str
    updated_at: Optional[str] = None


class AssistantJobListItem(BaseModel):
    """Summary item for job listing."""

    job_id: str
    user_id: str
    status: str
    request_type: Optional[str] = None
    progress: int = 0
    status_text: Optional[str] = None
    created_at: str
    updated_at: Optional[str] = None


class AssistantJobListResponse(BaseModel):
    """Response for listing jobs."""

    jobs: List[AssistantJobListItem]
    total: int


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


def _run_assistant_job(
    job_id: str,
    user_id: str,
    message: str,
    context: Dict[str, Any],
) -> None:
    """Background thread function that runs an assistant job with status updates."""
    try:
        update_job(job_id, status=PA_JOB_STATUS_RUNNING, status_text="Classifying intent...")

        if is_job_cancelled(job_id):
            return

        def job_updater(
            status_text: Optional[str] = None,
            progress: Optional[int] = None,
            request_type: Optional[str] = None,
        ) -> bool:
            """Callback for orchestrator to update job status. Returns False if cancelled."""
            if is_job_cancelled(job_id):
                return False
            updates: Dict[str, Any] = {}
            if status_text is not None:
                updates["status_text"] = status_text
            if progress is not None:
                updates["progress"] = progress
            if request_type is not None:
                updates["request_type"] = request_type
            if updates:
                update_job(job_id, **updates)
            return True

        request = AssistantRequest(
            request_id=job_id,
            user_id=user_id,
            message=message,
            context=context,
        )

        response = orchestrator.handle_request(request, job_updater=job_updater)

        if is_job_cancelled(job_id):
            return

        update_job(
            job_id,
            status=PA_JOB_STATUS_COMPLETED,
            progress=100,
            status_text="Request completed successfully",
            response=response.model_dump(),
        )
        if slack_notify_pa_response:
            threading.Thread(
                target=slack_notify_pa_response,
                args=(
                    user_id,
                    message,
                    response.message,
                    response.actions_taken,
                    response.follow_up_suggestions,
                ),
                daemon=True,
            ).start()

    except Exception as e:
        logger.exception("Job %s failed: %s", job_id, e)
        update_job(
            job_id,
            status=PA_JOB_STATUS_FAILED,
            status_text=f"Error: {str(e)}",
            error=str(e),
        )


@app.post("/assistant/jobs", response_model=AssistantJobResponse)
async def start_assistant_job(user_id: str, body: AssistantJobRequest):
    """
    Start an async assistant job.
    
    Returns a job_id that can be used to poll for status.
    If async_mode is False, runs synchronously and returns result directly.
    """
    job_id = str(uuid4())

    if not body.async_mode:
        request = AssistantRequest(
            request_id=job_id,
            user_id=user_id,
            message=body.message,
            context=body.context,
        )
        response = orchestrator.handle_request(request)
        create_job(
            job_id=job_id,
            user_id=user_id,
            request_type="assistant",
            message=body.message,
            context=body.context,
        )
        update_job(
            job_id,
            status=PA_JOB_STATUS_COMPLETED,
            progress=100,
            response=response.model_dump(),
        )
        if slack_notify_pa_response:
            threading.Thread(
                target=slack_notify_pa_response,
                args=(
                    user_id,
                    body.message,
                    response.message,
                    response.actions_taken,
                    response.follow_up_suggestions,
                ),
                daemon=True,
            ).start()
        return AssistantJobResponse(
            job_id=job_id,
            status=PA_JOB_STATUS_COMPLETED,
            message="Request completed synchronously.",
        )

    create_job(
        job_id=job_id,
        user_id=user_id,
        request_type="assistant",
        message=body.message,
        context=body.context,
    )

    try:
        from personal_assistant_team.temporal.client import is_temporal_enabled
        from personal_assistant_team.temporal.start_workflow import start_assistant_workflow
        if is_temporal_enabled():
            start_assistant_workflow(
                job_id, user_id, body.message,
                body.context if isinstance(body.context, dict) else (body.context or {}),
            )
            return AssistantJobResponse(job_id=job_id, status=PA_JOB_STATUS_RUNNING)
    except ImportError:
        pass

    thread = threading.Thread(
        target=_run_assistant_job,
        args=(job_id, user_id, body.message, body.context),
        daemon=True,
    )
    thread.start()

    return AssistantJobResponse(job_id=job_id, status=PA_JOB_STATUS_RUNNING)


@app.get("/assistant/jobs/{job_id}", response_model=AssistantJobStatus)
async def get_assistant_job_status(job_id: str):
    """
    Get the status of an assistant job.
    
    Poll this endpoint to track progress and retrieve results when completed.
    """
    job_data = get_job(job_id)
    if job_data is None:
        raise HTTPException(status_code=404, detail="Job not found")

    return AssistantJobStatus(
        job_id=job_data.get("job_id", job_id),
        user_id=job_data.get("user_id", ""),
        status=job_data.get("status", PA_JOB_STATUS_PENDING),
        request_type=job_data.get("request_type"),
        progress=job_data.get("progress", 0),
        status_text=job_data.get("status_text"),
        request_message=job_data.get("request_message", ""),
        response=job_data.get("response"),
        error=job_data.get("error"),
        created_at=job_data.get("created_at", ""),
        updated_at=job_data.get("updated_at"),
    )


@app.post("/assistant/jobs/{job_id}/cancel")
async def cancel_assistant_job(job_id: str):
    """
    Cancel a running or pending job.
    
    Returns success if the job was cancelled, or an error if it cannot be cancelled.
    """
    job_data = get_job(job_id)
    if job_data is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if cancel_job(job_id):
        return {"success": True, "message": "Job cancelled"}
    else:
        return {
            "success": False,
            "message": f"Cannot cancel job with status: {job_data.get('status')}",
        }


@app.delete("/assistant/jobs/{job_id}")
async def delete_assistant_job(job_id: str):
    """Delete an assistant job from the store. Returns 404 if not found."""
    job_data = get_job(job_id)
    if job_data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if not delete_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, "message": "Job deleted"}


@app.get("/users/{user_id}/assistant/jobs", response_model=AssistantJobListResponse)
async def list_user_assistant_jobs(
    user_id: str,
    running_only: bool = False,
    limit: int = 50,
):
    """
    List assistant jobs for a user.
    
    Use running_only=true to filter to only pending/running jobs.
    """
    jobs_data = list_jobs(user_id=user_id, running_only=running_only, limit=limit)
    
    items = [
        AssistantJobListItem(
            job_id=j.get("job_id", ""),
            user_id=j.get("user_id", ""),
            status=j.get("status", PA_JOB_STATUS_PENDING),
            request_type=j.get("request_type"),
            progress=j.get("progress", 0),
            status_text=j.get("status_text"),
            created_at=j.get("created_at", ""),
            updated_at=j.get("updated_at"),
        )
        for j in jobs_data
    ]

    return AssistantJobListResponse(jobs=items, total=len(items))


@app.post("/users/{user_id}/assistant", response_model=AssistantResponse)
async def assistant_request(user_id: str, body: AssistantRequestBody):
    """
    Send a free-form request to the personal assistant.
    
    The assistant will classify the intent and route to the appropriate agent.
    """
    request = AssistantRequest(
        request_id=str(uuid4()),
        user_id=user_id,
        message=body.message,
        context=body.context,
    )
    
    response = orchestrator.handle_request(request)
    if slack_notify_pa_response:
        threading.Thread(
            target=slack_notify_pa_response,
            args=(
                user_id,
                body.message,
                response.message,
                response.actions_taken,
                response.follow_up_suggestions,
            ),
            daemon=True,
        ).start()
    return response


@app.get("/users/{user_id}/profile")
async def get_profile(user_id: str):
    """Get the full user profile."""
    profile = profile_store.load_profile(user_id)
    if profile is None:
        profile = profile_store.create_profile(user_id)
    return profile.model_dump()


@app.post("/users/{user_id}/profile")
async def update_profile(user_id: str, body: ProfileUpdateBody):
    """Update a category of the user profile."""
    try:
        profile = profile_store.update_category(
            user_id=user_id,
            category=body.category,
            data=body.data,
            merge=body.merge,
        )
        return {"success": True, "profile": profile.model_dump()}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/users/{user_id}/profile/summary")
async def get_profile_summary(user_id: str):
    """Get a text summary of the user profile."""
    summary = profile_store.get_profile_summary(user_id)
    return {"summary": summary or "No profile information available."}


@app.post("/users/{user_id}/email/connect")
async def connect_email(user_id: str, body: EmailConnectBody):
    """Connect an email account."""
    from ..email_agent.models import ConnectEmailRequest
    
    try:
        result = orchestrator.email_agent.connect_email(ConnectEmailRequest(
            user_id=user_id,
            provider=body.provider,
            credentials=body.credentials,
        ))
        return {"success": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/users/{user_id}/email/inbox")
async def get_inbox(user_id: str, limit: int = 20, unread_only: bool = False):
    """Fetch emails from inbox."""
    from ..email_agent.models import EmailReadRequest
    
    if not orchestrator.email_agent.has_credentials(user_id):
        raise HTTPException(status_code=400, detail="Email not connected")
    
    try:
        emails = orchestrator.email_agent.read_emails(EmailReadRequest(
            user_id=user_id,
            limit=limit,
            unread_only=unread_only,
        ))
        return {"emails": [e.model_dump() for e in emails]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/users/{user_id}/email/draft")
async def create_email_draft(user_id: str, body: EmailDraftBody):
    """Create an email draft."""
    from ..email_agent.models import EmailDraftRequest
    
    draft = orchestrator.email_agent.draft_email(EmailDraftRequest(
        user_id=user_id,
        intent=body.intent,
        context=body.context,
    ))
    return {"draft": draft.model_dump()}


@app.get("/users/{user_id}/calendar/events")
async def list_calendar_events(
    user_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 50,
):
    """List calendar events."""
    from ..calendar_agent.models import ListEventsRequest
    
    start = datetime.fromisoformat(start_date) if start_date else None
    end = datetime.fromisoformat(end_date) if end_date else None
    
    try:
        events = orchestrator.calendar_agent.list_events(ListEventsRequest(
            user_id=user_id,
            start_date=start,
            end_date=end,
            limit=limit,
        ))
        return {"events": [e.model_dump() for e in events]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/users/{user_id}/calendar/events")
async def create_calendar_event(user_id: str, body: CalendarEventCreateBody):
    """Create a calendar event."""
    from ..calendar_agent.models import CreateEventRequest
    
    try:
        event_id = orchestrator.calendar_agent.create_event(CreateEventRequest(
            user_id=user_id,
            title=body.title,
            start_time=datetime.fromisoformat(body.start_time),
            end_time=datetime.fromisoformat(body.end_time) if body.end_time else None,
            duration_minutes=body.duration_minutes,
            location=body.location,
            description=body.description,
            attendees=body.attendees,
        ))
        return {"event_id": event_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/users/{user_id}/calendar/events/from-text")
async def create_event_from_text(user_id: str, body: EventFromTextBody):
    """Create a calendar event from natural language."""
    result = orchestrator.calendar_agent.create_event_from_text(
        user_id=user_id,
        text=body.text,
        auto_create=body.auto_create,
    )
    return result


@app.get("/users/{user_id}/tasks/lists")
async def list_task_lists(user_id: str):
    """Get all task lists for a user."""
    lists = orchestrator.task_agent.get_all_lists(user_id)
    return {"lists": [lst.model_dump() for lst in lists]}


@app.post("/users/{user_id}/tasks/lists")
async def create_task_list(user_id: str, body: TaskListCreateBody):
    """Create a new task list."""
    from ..task_agent.models import CreateListRequest
    
    task_list = orchestrator.task_agent.create_list(CreateListRequest(
        user_id=user_id,
        name=body.name,
        description=body.description,
    ))
    return {"list": task_list.model_dump()}


@app.get("/users/{user_id}/tasks/lists/{list_id}")
async def get_task_list(user_id: str, list_id: str):
    """Get a specific task list."""
    task_list = orchestrator.task_agent.get_list(user_id, list_id)
    if task_list is None:
        raise HTTPException(status_code=404, detail="List not found")
    return {"list": task_list.model_dump()}


@app.post("/users/{user_id}/tasks/lists/{list_id}/items")
async def add_task_item(user_id: str, list_id: str, body: TaskItemAddBody):
    """Add an item to a task list."""
    from ..task_agent.models import AddItemRequest
    from ..models import Priority
    
    item = orchestrator.task_agent.add_item(AddItemRequest(
        user_id=user_id,
        list_id=list_id,
        description=body.description,
        quantity=body.quantity,
        priority=Priority(body.priority),
        due_date=datetime.fromisoformat(body.due_date) if body.due_date else None,
        notes=body.notes,
    ))
    return {"item": item.model_dump()}


@app.post("/users/{user_id}/tasks/from-text")
async def add_tasks_from_text(user_id: str, body: TasksFromTextBody):
    """Add tasks from natural language."""
    from ..task_agent.models import AddItemsFromTextRequest
    
    result = orchestrator.task_agent.add_items_from_text(AddItemsFromTextRequest(
        user_id=user_id,
        text=body.text,
    ))
    return result


@app.post("/users/{user_id}/tasks/lists/{list_id}/items/{item_id}/complete")
async def complete_task_item(user_id: str, list_id: str, item_id: str):
    """Mark a task item as completed."""
    from ..task_agent.models import CompleteItemRequest
    
    success = orchestrator.task_agent.complete_item(CompleteItemRequest(
        user_id=user_id,
        list_id=list_id,
        item_id=item_id,
    ))
    return {"success": success}


@app.get("/users/{user_id}/tasks/pending")
async def get_pending_tasks(user_id: str):
    """Get all pending tasks across all lists."""
    items = orchestrator.task_agent.get_pending_items(user_id)
    return {"items": [item.model_dump() for item in items]}


@app.get("/users/{user_id}/deals")
async def get_deals(user_id: str, query: Optional[str] = None, max_results: int = 10):
    """Search for deals."""
    from ..deal_finder_agent.models import SearchDealsRequest
    
    result = orchestrator.deal_finder.search_deals(SearchDealsRequest(
        user_id=user_id,
        query=query,
        max_results=max_results,
    ))
    return {"deals": [d.model_dump() for d in result.deals], "query": result.query_used}


@app.get("/users/{user_id}/deals/personalized")
async def get_personalized_deals(user_id: str):
    """Get personalized deal recommendations."""
    deals = orchestrator.deal_finder.get_personalized_deals(user_id)
    return {"deals": [d.model_dump() for d in deals]}


@app.get("/users/{user_id}/deals/wishlist")
async def get_wishlist(user_id: str):
    """Get user's wishlist."""
    wishlist = orchestrator.deal_finder.get_wishlist(user_id)
    return {"wishlist": [w.model_dump() for w in wishlist]}


@app.post("/users/{user_id}/deals/wishlist")
async def add_to_wishlist(user_id: str, body: WishlistAddBody):
    """Add an item to wishlist."""
    from ..deal_finder_agent.models import AddWishlistRequest
    
    item = orchestrator.deal_finder.add_to_wishlist(AddWishlistRequest(
        user_id=user_id,
        description=body.description,
        target_price=body.target_price,
        category=body.category,
    ))
    return {"item": item.model_dump()}


@app.get("/users/{user_id}/reservations")
async def list_reservations(user_id: str, include_past: bool = False):
    """List user's reservations."""
    from ..reservation_agent.models import ListReservationsRequest
    
    reservations = orchestrator.reservation_agent.list_reservations(ListReservationsRequest(
        user_id=user_id,
        include_past=include_past,
    ))
    return {"reservations": [r.model_dump() for r in reservations]}


@app.post("/users/{user_id}/reservations")
async def create_reservation(user_id: str, body: ReservationCreateBody):
    """Create a reservation."""
    from ..reservation_agent.models import MakeReservationRequest
    from ..models import ReservationType
    
    result = orchestrator.reservation_agent.make_reservation(MakeReservationRequest(
        user_id=user_id,
        reservation_type=ReservationType(body.reservation_type),
        venue_name=body.venue_name,
        datetime=datetime.fromisoformat(body.datetime),
        party_size=body.party_size,
        notes=body.notes,
    ))
    return {"reservation": result.model_dump()}


@app.post("/users/{user_id}/reservations/from-text")
async def create_reservation_from_text(user_id: str, body: ReservationFromTextBody):
    """Create a reservation from natural language."""
    result = orchestrator.reservation_agent.create_reservation_from_text(
        user_id=user_id,
        text=body.text,
    )
    return result


@app.get("/users/{user_id}/reservations/restaurants/recommend")
async def recommend_restaurants(user_id: str, location: Optional[str] = None):
    """Get restaurant recommendations."""
    venues = orchestrator.reservation_agent.recommend_restaurants(
        user_id=user_id,
        location=location,
    )
    return {"venues": [v.model_dump() for v in venues]}


@app.post("/users/{user_id}/documents/process")
async def generate_process_doc(user_id: str, body: DocumentGenerateBody):
    """Generate a process document."""
    from ..doc_generator_agent.models import GenerateDocRequest
    
    doc = orchestrator.doc_generator.generate_process_doc(GenerateDocRequest(
        user_id=user_id,
        doc_type=body.doc_type,
        topic=body.topic,
        context=body.context,
    ))
    return {"document": doc.model_dump()}


@app.post("/users/{user_id}/documents/checklist")
async def generate_checklist(user_id: str, body: ChecklistGenerateBody):
    """Generate a checklist."""
    from ..doc_generator_agent.models import GenerateChecklistRequest
    
    checklist = orchestrator.doc_generator.generate_checklist(GenerateChecklistRequest(
        user_id=user_id,
        task=body.task,
        include_time_estimates=body.include_time_estimates,
    ))
    return {"checklist": checklist.model_dump()}


@app.get("/users/{user_id}/documents")
async def list_documents(user_id: str, doc_type: Optional[str] = None):
    """List generated documents."""
    docs = orchestrator.doc_generator.list_documents(user_id, doc_type)
    return {"documents": docs}


@app.get("/users/{user_id}/documents/{doc_id}")
async def get_document(user_id: str, doc_id: str):
    """Get a generated document."""
    content = orchestrator.doc_generator.get_document(user_id, doc_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"content": content}
