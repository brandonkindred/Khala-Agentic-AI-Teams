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
from branding_team.store import AttachConversationResult

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

    Preconditions:
        ``client_id`` is a non-empty path string; ``limit`` (when supplied) is
        ``> 0`` and ``offset`` is ``>= 0`` — FastAPI rejects violations with a
        422 before this body runs.
    Postconditions:
        Returns the client's brands (a possibly empty list), sliced by
        ``limit``/``offset`` when given. Raises 404 when no client row matches
        ``client_id``.
    """
    from branding_team.api import main as _main

    if not _main.branding_store.get_client(client_id):
        raise HTTPException(status_code=404, detail="Client not found")
    return _main.branding_store.list_brands_for_client(client_id, limit=limit, offset=offset)


@router.post("/clients/{client_id}/brands", response_model=Brand, status_code=201)
def create_brand(client_id: str, payload: CreateBrandRequest) -> Brand:
    """Create a brand for a client and bind it to exactly one conversation.

    The brand's mission is derived from ``payload`` via ``_mission_from_payload``.
    A conversation is resolved first — ``payload.conversation_id`` (stripped) if
    set, otherwise a fresh, unattached one via ``conversation_store.create`` — and
    then linked to the brand through the single atomic
    ``store.attach_conversation`` call, so both the provided-conversation and
    create-new cases share one linking step instead of two independently
    maintained ones.

    When the link step reports a failure — a non-OK ``attach_conversation``
    result, or ``attach_conversation``/``conversation_store.create`` raising —
    the just-created brand is rolled back with ``store.delete_brand`` so this
    handler never leaves a listable, conversation-less orphan. Because the
    conversation is created (or reused) unattached and only ever gains a
    ``brand_id`` inside ``attach_conversation``'s own transaction, there is no
    point at which a conversation is left pointing at a brand this handler is
    about to delete — unlike patching the brand's ``conversation_id`` and the
    conversation's ``brand_id`` as two separate writes, which would need its
    own compensation if the second write failed.

    Preconditions:
        ``client_id`` is a non-empty path string; ``payload`` is a validated
        ``CreateBrandRequest``.
    Postconditions:
        Returns the created ``Brand`` with its conversation attached (HTTP 201).
        Raises 404 "Client not found" when ``client_id`` matches no client.
        Resolves a conversation id — ``payload.conversation_id`` (stripped) if
        set, otherwise one freshly created unattached — deleting the brand and
        re-raising if that creation raises. Attaches it via
        ``store.attach_conversation``; on a non-OK result (or that call
        raising) it deletes the just-created brand, then maps a non-OK result
        to HTTP status: 404 for ``CONVERSATION_NOT_FOUND``/``BRAND_NOT_FOUND``
        and 409 for ``ALREADY_ATTACHED``, or re-raises the original exception.
    """
    from branding_team.api import main as _main

    mission = _mission_from_payload(payload)

    brand = _main.branding_store.create_brand(
        client_id=client_id, mission=mission, name=payload.name
    )
    if not brand:
        raise HTTPException(status_code=404, detail="Client not found")

    conversation_store = _main.conversation_store
    # Reuse the caller's conversation if provided, otherwise create a fresh,
    # unattached one — either way, attach_conversation below performs the one
    # atomic brand<->conversation link.
    conversation_id = (payload.conversation_id or "").strip() or None
    if not conversation_id:
        try:
            conversation_id = conversation_store.create(mission=mission)
        except Exception:
            _main.branding_store.delete_brand(client_id, brand.id)
            raise

    try:
        # Single-transaction attach: checks the uniqueness invariant and
        # writes both the conversation and brand rows atomically, so a
        # concurrent request can't attach the same conversation elsewhere
        # in between, and a failed brand patch can't leave the conversation
        # pointing at a brand that doesn't reference it back.
        result, attached_brand = _main.branding_store.attach_conversation(
            client_id, brand.id, conversation_id, mission
        )
    except Exception:
        _main.branding_store.delete_brand(client_id, brand.id)
        raise

    if result is not AttachConversationResult.OK:
        # The attach failed after create_brand already committed the brand
        # row above — roll it back so a failed request never leaves a
        # listable, conversation-less orphan behind.
        _main.branding_store.delete_brand(client_id, brand.id)
    if result is AttachConversationResult.CONVERSATION_NOT_FOUND:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if result is AttachConversationResult.ALREADY_ATTACHED:
        raise HTTPException(
            status_code=409,
            detail="Conversation is already attached to another brand",
        )
    if result is AttachConversationResult.BRAND_NOT_FOUND:
        raise HTTPException(status_code=404, detail="Brand not found")
    return attached_brand


@router.get("/clients/{client_id}/brands/{brand_id}", response_model=Brand)
def get_brand(client_id: str, brand_id: str) -> Brand:
    """Fetch a single brand belonging to a client.

    Preconditions:
        ``client_id`` and ``brand_id`` are non-empty path strings.
    Postconditions:
        Returns the ``Brand`` when it exists under ``client_id``. Raises 404
        "Brand not found" otherwise (including when the brand exists but belongs
        to a different client).
    """
    from branding_team.api import main as _main

    brand = _main.branding_store.get_brand(client_id, brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return brand


@router.put("/clients/{client_id}/brands/{brand_id}", response_model=Brand)
def update_brand(client_id: str, brand_id: str, payload: UpdateBrandRequest) -> Brand:
    """Patch a brand's mission fields, status, and/or name.

    The mission patch is derived from the payload's own field set —
    ``payload.model_dump(exclude_none=True, exclude={"status", "name"})`` — so it
    tracks ``UpdateBrandRequest`` automatically rather than a hand-maintained
    list. A no-op guard drops the mission when the derived value equals the
    current one, so a status/name-only edit does not needlessly invalidate the
    brand's cached generated output in the store (``store.update_brand`` clears
    ``latest_output`` and resets ``current_phase`` whenever a mission is passed).

    Preconditions:
        ``client_id`` and ``brand_id`` are non-empty path strings; ``payload`` is
        a validated ``UpdateBrandRequest``.
    Postconditions:
        Returns the updated ``Brand``. Raises 404 "Brand not found" when the brand
        does not exist under ``client_id`` (checked up front and again if the
        store write finds no row). Raises 400 "Invalid status: …" when
        ``payload.status`` is set but does not name a valid ``BrandStatus``.
        Forwards a mission to the store only when it differs from the brand's
        current mission, keeping a status/name-only update idempotent with
        respect to generated artifacts.
    """
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
    """Return the single conversation for a brand.

    Preconditions:
        ``client_id`` and ``brand_id`` are non-empty path strings.
    Postconditions:
        Returns the ``ConversationStateResponse`` for the brand's one attached
        conversation. Raises 404 "Brand not found" when the brand is unknown, and
        404 "Brand has no conversation" when the brand exists but has none
        attached.
    """
    from branding_team.api import main as _main

    brand = _main.branding_store.get_brand(client_id, brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    result = _main.conversation_store.get_by_brand_id(brand_id)
    if not result:
        raise HTTPException(status_code=404, detail="Brand has no conversation")
    cid, messages, mission, latest_output = result
    return _conversation_to_response(cid, brand_id, messages, mission, latest_output, [])
