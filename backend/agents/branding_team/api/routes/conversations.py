"""Branding API — conversation (chat) endpoints.

The async wrappers here dispatch the blocking conversation bodies
(``api.conversation._*_impl``) onto the bounded pipeline executor so a
multi-minute pipeline run never holds an event-loop worker.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional

from fastapi import APIRouter, Body, HTTPException

from branding_team.api import background as _bg
from branding_team.api import main as _main
from branding_team.api.conversation import (
    _brand_exists,
    _conversation_to_response,
    _create_branding_conversation_impl,
    _send_branding_conversation_message_impl,
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
    """
    return await _bg._run_in_pipeline_executor(
        _send_branding_conversation_message_impl, conversation_id, payload
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationStateResponse)
def get_branding_conversation(conversation_id: str) -> ConversationStateResponse:
    """Return the full stored state (messages, mission, output, brand) for a
    conversation in a single query; 404 if it does not exist."""
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
    resolving each attached brand's name in a single batched lookup."""
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
    updated state. 404 if either the brand or the conversation is unknown."""
    brand_id = payload.brand_id.strip()
    if not _brand_exists(brand_id):
        raise HTTPException(status_code=404, detail="Brand not found")
    state = _main.conversation_store.get_state(conversation_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    _main.conversation_store.set_brand(conversation_id, brand_id)
    return _conversation_to_response(
        conversation_id, brand_id, state.messages, state.mission, state.latest_output, []
    )
