"""Product Delivery — backlog CRUD, grooming, feedback intake.

Phase 1 of issue #243. Mounted under ``/api/product-delivery`` directly
on the unified API (this is an in-process module, not a proxy team).
Sprint planning, releases, and the SE-pipeline integration ship in
follow-up issues.

Domain exceptions raised by ``product_delivery.store`` are mapped to
HTTP statuses by the app-level handlers registered via
:func:`register_pd_exception_handlers`. Routes therefore call the store
directly and let the handlers translate failures — no per-route
try/except chains.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from product_delivery import (
    AcceptanceCriterion,
    BacklogTree,
    CrossProductFeedbackLink,
    Epic,
    FeedbackItem,
    GroomRequest,
    GroomResult,
    Initiative,
    Product,
    ProductDeliveryStorageUnavailable,
    Sprint,
    SprintPlanRequest,
    SprintPlanResult,
    SprintWithStories,
    Story,
    StoryAlreadyPlanned,
    Task,
    UnknownProductDeliveryEntity,
    get_store,
    resolve_author,
)
from product_delivery.models import (
    AcceptanceCriterionCreate,
    CreateSprintRequest,
    EpicCreate,
    FeedbackItemCreate,
    InitiativeCreate,
    ProductCreate,
    ScoreUpdate,
    StatusUpdate,
    StoryCreate,
    TaskCreate,
)
from product_delivery.product_owner_agent import ProductOwnerAgent
from product_delivery.product_owner_agent.agent import LLMScoringUnavailable
from product_delivery.sprint_planner_agent import SprintPlannerAgent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/product-delivery", tags=["product-delivery"])


# ---------------------------------------------------------------------------
# Exception handlers — registered on the FastAPI app, not the router
# (APIRouter doesn't support exception_handler). The mapping is the single
# source of truth for status codes; tests register it the same way.
# ---------------------------------------------------------------------------


_EXC_STATUS: dict[type[Exception], int] = {
    CrossProductFeedbackLink: 400,
    UnknownProductDeliveryEntity: 404,
    # `UNIQUE(story_id)` on `product_delivery_sprint_stories` enforces
    # one-sprint-per-story; concurrent planners or explicit re-plans
    # surface here as 409 instead of a raw 500.
    StoryAlreadyPlanned: 409,
    ProductDeliveryStorageUnavailable: 503,
    # LLM transport/model/parse failures during /groom — clients retry
    # the same way they do for a Postgres outage.
    LLMScoringUnavailable: 503,
}


def register_pd_exception_handlers(app: FastAPI) -> None:
    for exc_cls, status_code in _EXC_STATUS.items():

        @app.exception_handler(exc_cls)
        async def _handler(_req: Request, exc: Exception, _s: int = status_code) -> JSONResponse:
            return JSONResponse({"detail": str(exc)}, status_code=_s)


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


@router.post("/products", response_model=Product)
def create_product(body: ProductCreate) -> Product:
    return get_store().create_product(
        name=body.name,
        description=body.description,
        vision=body.vision,
        author=resolve_author(),
    )


@router.get("/products", response_model=list[Product])
def list_products() -> list[Product]:
    return get_store().list_products()


@router.get("/products/{product_id}/backlog", response_model=BacklogTree)
def get_backlog(product_id: str) -> BacklogTree:
    tree = get_store().get_backlog_tree(product_id)
    if tree is None:
        raise HTTPException(status_code=404, detail=f"unknown product: {product_id}")
    return tree


# ---------------------------------------------------------------------------
# Backlog hierarchy CRUD
# ---------------------------------------------------------------------------


@router.post("/initiatives", response_model=Initiative)
def create_initiative(body: InitiativeCreate) -> Initiative:
    return get_store().create_initiative(
        product_id=body.product_id,
        title=body.title,
        summary=body.summary,
        status=body.status,
        author=resolve_author(),
    )


@router.post("/epics", response_model=Epic)
def create_epic(body: EpicCreate) -> Epic:
    return get_store().create_epic(
        initiative_id=body.initiative_id,
        title=body.title,
        summary=body.summary,
        status=body.status,
        author=resolve_author(),
    )


@router.post("/stories", response_model=Story)
def create_story(body: StoryCreate) -> Story:
    return get_store().create_story(
        epic_id=body.epic_id,
        title=body.title,
        user_story=body.user_story,
        status=body.status,
        estimate_points=body.estimate_points,
        author=resolve_author(),
    )


@router.post("/tasks", response_model=Task)
def create_task(body: TaskCreate) -> Task:
    return get_store().create_task(
        story_id=body.story_id,
        title=body.title,
        description=body.description,
        status=body.status,
        owner=body.owner,
        author=resolve_author(),
    )


@router.post("/acceptance-criteria", response_model=AcceptanceCriterion)
def create_acceptance_criterion(
    body: AcceptanceCriterionCreate,
) -> AcceptanceCriterion:
    return get_store().create_acceptance_criterion(
        story_id=body.story_id,
        text=body.text,
        satisfied=body.satisfied,
        author=resolve_author(),
    )


_StatusKind = Literal["initiative", "epic", "story", "task"]
_ScoredKind = Literal["initiative", "epic", "story"]


@router.patch("/{kind}/{entity_id}/status")
def patch_status(kind: _StatusKind, entity_id: str, body: StatusUpdate) -> dict[str, Any]:
    if not get_store().update_status(kind=kind, entity_id=entity_id, status=body.status):
        raise HTTPException(status_code=404, detail=f"unknown {kind}: {entity_id}")
    return {"ok": True, "kind": kind, "id": entity_id, "status": body.status}


@router.patch("/{kind}/{entity_id}/scores")
def patch_scores(kind: _ScoredKind, entity_id: str, body: ScoreUpdate) -> dict[str, Any]:
    # Distinguish "bad request" from "not found" so clients can branch
    # correctly on retry / create flows. An empty body is a 400 even if
    # the entity exists; an unknown id is a 404.
    if body.wsjf_score is None and body.rice_score is None:
        raise HTTPException(
            status_code=400,
            detail="at least one of wsjf_score / rice_score must be supplied",
        )
    ok = get_store().update_scores(
        kind=kind,
        entity_id=entity_id,
        wsjf_score=body.wsjf_score,
        rice_score=body.rice_score,
    )
    if not ok:
        raise HTTPException(status_code=404, detail=f"unknown {kind}: {entity_id}")
    return {"ok": True, "kind": kind, "id": entity_id}


# ---------------------------------------------------------------------------
# Grooming
# ---------------------------------------------------------------------------


def _llm_client_factory() -> Any:
    """Bootstrap the LLM client lazily.

    Both the import and the ``get_client`` call can fail (missing strands
    plugin, bad credentials, broken provider). Wrapping them in a single
    callable lets the agent invoke it only when the backlog is non-empty,
    and lets the agent's existing ``LLMScoringUnavailable`` path handle
    failures without an extra try/except in the route. Tests stub by
    monkeypatching ``sys.modules['llm_service']``.
    """
    from llm_service import get_client  # noqa: PLC0415 — tests stub via sys.modules

    return get_client("product_owner")


@router.post("/groom", response_model=GroomResult)
def groom(body: GroomRequest) -> GroomResult:
    store = get_store()
    # No standalone existence check here — `agent.groom` calls
    # `store.list_stories_for_product`, which raises
    # `UnknownProductDeliveryEntity` (mapped to 404 by the global
    # handler) inside a single transaction with the product-existence
    # SELECT. So a concurrent delete can't slip past as `200 []`.
    #
    # Factory failures and LLM call failures both surface as
    # `LLMScoringUnavailable`, mapped to 503 by the global handler.
    agent = ProductOwnerAgent(store=store, llm_factory=_llm_client_factory)
    return agent.groom(
        product_id=body.product_id,
        method=body.method,
        persist=body.persist,
    )


# ---------------------------------------------------------------------------
# Feedback intake
# ---------------------------------------------------------------------------


@router.post("/feedback", response_model=FeedbackItem)
def create_feedback(body: FeedbackItemCreate) -> FeedbackItem:
    return get_store().create_feedback_item(
        product_id=body.product_id,
        source=body.source,
        raw_payload=body.raw_payload,
        severity=body.severity,
        linked_story_id=body.linked_story_id,
        author=resolve_author(),
    )


@router.get("/feedback", response_model=list[FeedbackItem])
def list_feedback(
    product_id: str,
    status: str | None = None,
) -> list[FeedbackItem]:
    # `store.list_feedback` checks product existence in the same
    # transaction as the SELECT and raises `UnknownProductDeliveryEntity`
    # (→ 404) when the product is missing — no TOCTTOU window where a
    # concurrent delete could turn a 404 into a `200 []`.
    return get_store().list_feedback(product_id, status=status)


# ---------------------------------------------------------------------------
# Sprints (Phase 2 of #243)
# ---------------------------------------------------------------------------


@router.post("/sprints", response_model=Sprint)
def create_sprint(body: CreateSprintRequest) -> Sprint:
    return get_store().create_sprint(
        product_id=body.product_id,
        name=body.name,
        capacity_points=body.capacity_points,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        status=body.status,
        author=resolve_author(),
    )


@router.post("/sprints/{sprint_id}/plan", response_model=SprintPlanResult)
def plan_sprint(sprint_id: str, body: SprintPlanRequest) -> SprintPlanResult:
    """Run capacity-aware story selection for ``sprint_id``.

    A missing sprint surfaces as 404 via ``UnknownProductDeliveryEntity``
    raised inside the agent (delegating to ``select_sprint_scope`` /
    ``get_sprint``). ``capacity_points=None`` falls back to the
    sprint row's stored capacity.
    """
    agent = SprintPlannerAgent(store=get_store())
    return agent.plan(sprint_id=sprint_id, capacity_points=body.capacity_points)


@router.get("/sprints/{sprint_id}", response_model=SprintWithStories)
def get_sprint(sprint_id: str) -> SprintWithStories:
    result = get_store().get_sprint_with_stories(sprint_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"unknown sprint: {sprint_id}")
    return result
