"""Product Delivery — backlog CRUD, grooming, feedback intake.

Phase 1 of issue #243. Mounted under ``/api/product-delivery`` directly
on the unified API (this is an in-process module, not a proxy team).
Sprint planning, releases, and the SE-pipeline integration ship in
follow-up issues.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException

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
    Story,
    Task,
    UnknownProductDeliveryEntity,
    get_store,
    resolve_author,
)
from product_delivery.models import (
    AcceptanceCriterionCreate,
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/product-delivery", tags=["product-delivery"])


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


@router.post("/products", response_model=Product)
def create_product(body: ProductCreate) -> Product:
    try:
        return get_store().create_product(
            name=body.name,
            description=body.description,
            vision=body.vision,
            author=resolve_author(),
        )
    except ProductDeliveryStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/products", response_model=list[Product])
def list_products() -> list[Product]:
    try:
        return get_store().list_products()
    except ProductDeliveryStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/products/{product_id}/backlog", response_model=BacklogTree)
def get_backlog(product_id: str) -> BacklogTree:
    try:
        tree = get_store().get_backlog_tree(product_id)
    except ProductDeliveryStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if tree is None:
        raise HTTPException(status_code=404, detail=f"unknown product: {product_id}")
    return tree


# ---------------------------------------------------------------------------
# Backlog hierarchy CRUD
# ---------------------------------------------------------------------------


@router.post("/initiatives", response_model=Initiative)
def create_initiative(body: InitiativeCreate) -> Initiative:
    try:
        return get_store().create_initiative(
            product_id=body.product_id,
            title=body.title,
            summary=body.summary,
            status=body.status,
            author=resolve_author(),
        )
    except UnknownProductDeliveryEntity as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProductDeliveryStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/epics", response_model=Epic)
def create_epic(body: EpicCreate) -> Epic:
    try:
        return get_store().create_epic(
            initiative_id=body.initiative_id,
            title=body.title,
            summary=body.summary,
            status=body.status,
            author=resolve_author(),
        )
    except UnknownProductDeliveryEntity as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProductDeliveryStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/stories", response_model=Story)
def create_story(body: StoryCreate) -> Story:
    try:
        return get_store().create_story(
            epic_id=body.epic_id,
            title=body.title,
            user_story=body.user_story,
            status=body.status,
            estimate_points=body.estimate_points,
            author=resolve_author(),
        )
    except UnknownProductDeliveryEntity as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProductDeliveryStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/tasks", response_model=Task)
def create_task(body: TaskCreate) -> Task:
    try:
        return get_store().create_task(
            story_id=body.story_id,
            title=body.title,
            description=body.description,
            status=body.status,
            owner=body.owner,
            author=resolve_author(),
        )
    except UnknownProductDeliveryEntity as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProductDeliveryStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/acceptance-criteria", response_model=AcceptanceCriterion)
def create_acceptance_criterion(
    body: AcceptanceCriterionCreate,
) -> AcceptanceCriterion:
    try:
        return get_store().create_acceptance_criterion(
            story_id=body.story_id,
            text=body.text,
            satisfied=body.satisfied,
            author=resolve_author(),
        )
    except UnknownProductDeliveryEntity as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProductDeliveryStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


_StatusKind = Literal["initiative", "epic", "story", "task"]
_ScoredKind = Literal["initiative", "epic", "story"]


@router.patch("/{kind}/{entity_id}/status")
def patch_status(kind: _StatusKind, entity_id: str, body: StatusUpdate) -> dict[str, Any]:
    try:
        ok = get_store().update_status(kind=kind, entity_id=entity_id, status=body.status)
    except ProductDeliveryStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not ok:
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
    try:
        ok = get_store().update_scores(
            kind=kind,
            entity_id=entity_id,
            wsjf_score=body.wsjf_score,
            rice_score=body.rice_score,
        )
    except ProductDeliveryStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail=f"unknown {kind}: {entity_id}")
    return {"ok": True, "kind": kind, "id": entity_id}


# ---------------------------------------------------------------------------
# Grooming
# ---------------------------------------------------------------------------


@router.post("/groom", response_model=GroomResult)
def groom(body: GroomRequest) -> GroomResult:
    try:
        store = get_store()
        if store.get_product(body.product_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown product: {body.product_id}")

        from llm_service import get_client  # noqa: PLC0415 — lazy: tests stub via override

        agent = ProductOwnerAgent(store=store, llm_client=get_client("product_owner"))
        return agent.groom(
            product_id=body.product_id,
            method=body.method,
            persist=body.persist,
        )
    except ProductDeliveryStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Feedback intake
# ---------------------------------------------------------------------------


@router.post("/feedback", response_model=FeedbackItem)
def create_feedback(body: FeedbackItemCreate) -> FeedbackItem:
    try:
        return get_store().create_feedback_item(
            product_id=body.product_id,
            source=body.source,
            raw_payload=body.raw_payload,
            severity=body.severity,
            linked_story_id=body.linked_story_id,
            author=resolve_author(),
        )
    except CrossProductFeedbackLink as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnknownProductDeliveryEntity as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProductDeliveryStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/feedback", response_model=list[FeedbackItem])
def list_feedback(
    product_id: str,
    status: str | None = None,
) -> list[FeedbackItem]:
    try:
        return get_store().list_feedback(product_id, status=status)
    except ProductDeliveryStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
