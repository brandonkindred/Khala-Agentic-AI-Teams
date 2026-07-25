"""Branding API — brand CRUD endpoints and the brand's conversation lookup.

``main`` is imported inside each handler, not at module scope: ``main`` mounts
this router at the bottom of its own import (after ``app`` and its globals are
defined), so a module-scope ``from branding_team.api import main`` here would
form a load-time cycle — this module would be re-entered by main before
``router`` (line below) is even defined. The function-local import keeps this
router importable in any order.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query

from branding_team.api.conversation import _conversation_to_response
from branding_team.api.models import (
    ConversationStateResponse,
    CreateBrandRequest,
    UpdateBrandRequest,
)
from branding_team.api.state import _mission_from_payload
from branding_team.models import Brand, BrandStatus

router = APIRouter()


@router.get("/clients/{client_id}/brands", response_model=List[Brand])
def list_brands(
    client_id: str,
    limit: Optional[int] = Query(None, gt=0),
    offset: int = Query(0, ge=0),
) -> List[Brand]:
    """List a client's brands, optionally paginated (404 if the client is unknown).

    ``limit``/``offset`` are validated by FastAPI (``gt=0`` / ``ge=0``) so bad
    input is a 422, not a 500 from the store's pagination guard.
    """
    from branding_team.api import main as _main

    if not _main.branding_store.get_client(client_id):
        raise HTTPException(status_code=404, detail="Client not found")
    return _main.branding_store.list_brands_for_client(client_id, limit=limit, offset=offset)


@router.post("/clients/{client_id}/brands", response_model=Brand, status_code=201)
def create_brand(client_id: str, payload: CreateBrandRequest) -> Brand:
    from branding_team.api import main as _main
    from branding_team.assistant.store import (
        ConversationAttachOutcome,
        _attach_conversation_to_brand,
    )
    from branding_team.store import _apply_brand_patch, _now_iso

    mission = _mission_from_payload(payload)

    brand = _main.branding_store.create_brand(
        client_id=client_id, mission=mission, name=payload.name
    )
    if not brand:
        raise HTTPException(status_code=404, detail="Client not found")

    conversation_store = _main.conversation_store
    # Attach an existing conversation if provided, otherwise create a new one.
    existing_conv_id = (payload.conversation_id or "").strip() or None
    if existing_conv_id and conversation_store.get(existing_conv_id) is not None:
        # Lock the conversation row, re-check its brand attachment, and update
        # both the conversation and the brand in one shared transaction — a
        # concurrent attach attempt blocks on the row lock instead of racing
        # past a stale check, and a failure mid-way rolls back both writes
        # instead of leaving the conversation linked to a brand that doesn't
        # reference it back.
        with conversation_store._transaction() as cur:
            outcome = _attach_conversation_to_brand(cur, existing_conv_id, brand.id, mission)
            if outcome is ConversationAttachOutcome.NOT_FOUND:
                raise HTTPException(status_code=404, detail="Conversation not found")
            if outcome is ConversationAttachOutcome.ALREADY_ATTACHED:
                raise HTTPException(
                    status_code=409,
                    detail="Conversation is already attached to another brand",
                )
            patch = {"updated_at": _now_iso(), "conversation_id": existing_conv_id}
            updated_brand = _apply_brand_patch(cur, brand.id, client_id, patch)
        if not updated_brand:
            raise HTTPException(status_code=404, detail="Brand not found")
        return updated_brand

    conv_id = conversation_store.create(brand_id=brand.id, mission=mission)
    brand = _main.branding_store.update_brand(client_id, brand.id, conversation_id=conv_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")

    return brand


@router.get("/clients/{client_id}/brands/{brand_id}", response_model=Brand)
def get_brand(client_id: str, brand_id: str) -> Brand:
    from branding_team.api import main as _main

    brand = _main.branding_store.get_brand(client_id, brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return brand


@router.put("/clients/{client_id}/brands/{brand_id}", response_model=Brand)
def update_brand(client_id: str, brand_id: str, payload: UpdateBrandRequest) -> Brand:
    from branding_team.api import main as _main

    brand = _main.branding_store.get_brand(client_id, brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")

    # Derive the mission patch from the payload's own field set (excluding the
    # two non-mission fields) instead of a hand-maintained list of mission
    # field names — keeps this in sync with UpdateBrandRequest automatically.
    mission_patch = payload.model_dump(exclude_none=True, exclude={"status", "name"})
    mission = brand.mission.model_copy(update=mission_patch) if mission_patch else None
    # A full-form PUT may resend unchanged mission fields alongside a
    # status/name edit. Only forward a mission to the store when its content
    # actually differs — passing an (unchanged) mission would needlessly
    # invalidate the generated output there (see update_brand), making an
    # otherwise idempotent update discard cached brand artifacts.
    if mission is not None and mission == brand.mission:
        mission = None

    status = None
    if payload.status is not None:
        try:
            status = BrandStatus(payload.status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {payload.status}")
    updated = _main.branding_store.update_brand(
        client_id=client_id,
        brand_id=brand_id,
        mission=mission,
        status=status,
        name=payload.name,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Brand not found")
    return updated


@router.get(
    "/clients/{client_id}/brands/{brand_id}/conversation", response_model=ConversationStateResponse
)
def get_brand_conversation(client_id: str, brand_id: str) -> ConversationStateResponse:
    """Return the single conversation for a brand."""
    from branding_team.api import main as _main

    brand = _main.branding_store.get_brand(client_id, brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    result = _main.conversation_store.get_by_brand_id(brand_id)
    if not result:
        raise HTTPException(status_code=404, detail="Brand has no conversation")
    cid, messages, mission, latest_output = result
    return _conversation_to_response(cid, brand_id, messages, mission, latest_output, [])
