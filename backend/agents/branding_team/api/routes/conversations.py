"""Branding API — conversation (chat) endpoints.

The async wrappers here dispatch the blocking conversation bodies
(``api.conversation._*_impl``) onto the bounded pipeline executor so a
multi-minute pipeline run never holds an event-loop worker.

``main`` is imported inside each handler, not at module scope: ``main`` mounts
this router at the bottom of its own import (after ``app`` and its globals are
defined), so a module-scope ``from branding_team.api import main`` here would
form a load-time cycle — this module would be re-entered by main before
``router`` (line below) is even defined. The function-local import keeps this
router importable in any order. ``background`` and the conversation helper
functions used by the handlers have no such restriction — ``background`` and
``conversation`` do not import ``main`` at module scope — so they stay
imported at module scope here.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional

from fastapi import APIRouter, Body, HTTPException

from branding_team.api import background as _bg
from branding_team.api.conversation import (
    _conversation_to_response,
    _create_branding_conversation_impl,
    _send_branding_conversation_message_impl,
    link_conversation_to_brand,
)
from branding_team.api.models import (
    AttachConversationBrandRequest,
    ConversationStateResponse,
    ConversationSummaryResponse,
    CreateConversationRequest,
    SendMessageRequest,
)

router = APIRouter()


@router.post("/conversations", response_model=ConversationStateResponse)
async def create_branding_conversation(
    body: Optional[CreateConversationRequest] = Body(default=None),
) -> ConversationStateResponse:
    """Create a conversation, optionally seeding it with an initial message.

    Only the initial-message path runs the assistant (two LLM calls) and may run
    the full ~40-agent pipeline, so it goes on the bounded pipeline executor
    (see ``_run_in_pipeline_executor``). The no-initial-message path only creates
    a conversation row and persists a greeting, so it stays off that pool — where
    it could otherwise queue behind multi-minute pipeline runs and make opening a
    fresh chat hang under load — and runs on the default executor instead.

    Preconditions:
        ``body`` is either ``None`` or a validated ``CreateConversationRequest``;
        a ``None`` body is treated as an empty request.
    Postconditions:
        Returns the new conversation's ``ConversationStateResponse``, including the
        assistant's reply when an initial message was supplied. Runs the blocking
        body off the event loop either way (pipeline executor when an initial
        message is present, default executor otherwise).
    """
    req = body or CreateConversationRequest()
    if (req.initial_message or "").strip():
        return await _bg._run_in_pipeline_executor(_create_branding_conversation_impl, req)
    return await asyncio.to_thread(_create_branding_conversation_impl, req)


@router.post("/conversations/{conversation_id}/messages", response_model=ConversationStateResponse)
async def send_branding_conversation_message(
    conversation_id: str, payload: SendMessageRequest
) -> ConversationStateResponse:
    """Append a user message, get the assistant's reply, and return updated state.

    Runs the assistant on the latest turn, persists the mission/output it
    derives, auto-creates and links a brand once enough info is present (unless
    ``skip_save``), and returns the refreshed conversation (404 if unknown).

    The assistant (two LLM calls) and any pipeline run are blocking, so the body
    executes on the bounded pipeline executor (see ``_run_in_pipeline_executor``)
    to keep the request from holding a worker thread — or the shared default
    executor — for the full pipeline duration.

    Preconditions:
        ``conversation_id`` is a non-empty path string; ``payload`` is a validated
        ``SendMessageRequest``.
    Postconditions:
        Returns the refreshed ``ConversationStateResponse`` with the assistant's
        reply and any derived mission/output persisted. Auto-creates and links a
        brand once enough information is present, unless ``payload.skip_save``.
        Raises 404 when the conversation is unknown.
    """
    return await _bg._run_in_pipeline_executor(
        _send_branding_conversation_message_impl, conversation_id, payload
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationStateResponse)
def get_branding_conversation(conversation_id: str) -> ConversationStateResponse:
    """Return the full stored state (messages, mission, output, brand) for a
    conversation in a single query; 404 if it does not exist.

    Preconditions:
        ``conversation_id`` is a non-empty path string.
    Postconditions:
        Returns the conversation's ``ConversationStateResponse`` assembled from a
        single ``get_state`` read. Raises 404 "Conversation not found" when no
        such conversation exists.
    """
    from branding_team.api import main as _main

    state = _main.conversation_store.get_state(conversation_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return _conversation_to_response(
        conversation_id,
        state.brand_id,
        state.messages,
        state.mission,
        state.latest_output,
        [],
    )


@router.get("/conversations", response_model=List[ConversationSummaryResponse])
def list_branding_conversations(
    brand_id: Optional[str] = None,
) -> List[ConversationSummaryResponse]:
    """List conversation summaries (optionally filtered by ``brand_id``),
    resolving each attached brand's name in a single batched lookup.

    Preconditions:
        ``brand_id``, when supplied, is a non-empty string.
    Postconditions:
        Returns the matching ``ConversationSummaryResponse`` list (empty when none
        match). Each summary's ``brand_name`` is resolved via one batched
        ``get_brand_names`` lookup over only the referenced brand ids, and is
        ``None`` for conversations with no attached brand.
    """
    from branding_team.api import main as _main

    summaries = _main.conversation_store.list_conversations(brand_id=brand_id)
    # Resolve only the brand names referenced by these conversations instead
    # of loading every brand of every client into memory.
    brand_names = _main.branding_store.get_brand_names(
        [s.brand_id for s in summaries if s.brand_id]
    )
    return [
        ConversationSummaryResponse(
            conversation_id=s.conversation_id,
            brand_id=s.brand_id,
            brand_name=brand_names.get(s.brand_id) if s.brand_id else None,
            created_at=s.created_at,
            updated_at=s.updated_at,
            message_count=s.message_count,
        )
        for s in summaries
    ]


@router.post("/conversations/{conversation_id}/brand", response_model=ConversationStateResponse)
def attach_conversation_to_brand(
    conversation_id: str, payload: AttachConversationBrandRequest
) -> ConversationStateResponse:
    """Attach an existing conversation to an existing brand and return the
    updated state. 404 if either the brand or the conversation is unknown.

    Goes through ``link_conversation_to_brand`` — the same atomic
    ``BrandingStore.attach_conversation`` path ``create_brand`` uses — instead
    of the one-sided ``ConversationStore.set_brand`` this endpoint used to call
    directly. That is a deliberate behavior change (issue #7084): a conversation
    already attached to a *different* brand is now rejected with 409 instead of
    being silently moved, which used to leave that other brand's
    ``conversation_id`` pointing at a conversation that no longer points back.
    Re-attaching a conversation to the brand it is already on remains a no-op
    success (``attach_conversation`` treats a matching ``brand_id`` as OK, not
    ``ALREADY_ATTACHED``).

    Preconditions:
        ``conversation_id`` is a non-empty path string; ``payload`` is a validated
        ``AttachConversationBrandRequest`` whose ``brand_id`` is non-empty after
        stripping.
    Postconditions:
        Returns the conversation's ``ConversationStateResponse`` now pointing at
        ``payload.brand_id``. Raises 404 "Brand not found" when the brand does
        not exist, 404 "Conversation not found" when the conversation is
        missing, and 409 "Conversation is already attached to another brand"
        when it is currently linked to a different brand.

        The conversation's mission is left untouched by this endpoint — no
        mission is passed to ``link_conversation_to_brand``, so the attach
        transaction preserves whatever mission is currently on the row instead
        of writing back the pre-lock ``state.mission`` snapshot taken below
        (which a concurrent ``POST /conversations/{id}/messages`` turn could
        have already superseded). The response is built from a fresh
        ``get_state`` read taken after the attach commits, so it reflects the
        committed mission rather than that snapshot.
    """
    from branding_team.api import main as _main

    brand_id = payload.brand_id.strip()
    resolved_brand = _main.branding_store.get_brand_by_id(brand_id)
    if resolved_brand is None:
        raise HTTPException(status_code=404, detail="Brand not found")
    client_id, _brand = resolved_brand
    state = _main.conversation_store.get_state(conversation_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    attached_brand = link_conversation_to_brand(client_id, brand_id, conversation_id)
    refreshed = _main.conversation_store.get_state(conversation_id) or state
    return _conversation_to_response(
        conversation_id,
        attached_brand.id,
        refreshed.messages,
        refreshed.mission,
        refreshed.latest_output,
        [],
    )
