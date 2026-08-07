"""Agentic team provisioning API — conversation endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from agent_team_studio.agentic_team_provisioning.api.services import conversations as conv_svc
from agent_team_studio.agentic_team_provisioning.models import (
    ConversationStateResponse,
    ConversationSummaryResponse,
    CreateConversationRequest,
    SendMessageRequest,
    SetConversationProcessRequest,
)

router = APIRouter()


@router.post("/conversations", response_model=ConversationStateResponse)
def create_conversation(req: CreateConversationRequest):
    return conv_svc.create_conversation(req)


@router.post("/conversations/{conversation_id}/messages", response_model=ConversationStateResponse)
def send_message(conversation_id: str, req: SendMessageRequest):
    return conv_svc.send_message(conversation_id, req)


@router.put("/conversations/{conversation_id}/process")
def set_conversation_process(conversation_id: str, req: SetConversationProcessRequest):
    return conv_svc.set_conversation_process(conversation_id, req)


@router.get("/conversations/{conversation_id}", response_model=ConversationStateResponse)
def get_conversation(conversation_id: str):
    return conv_svc.get_conversation(conversation_id)


@router.get("/teams/{team_id}/conversations", response_model=list[ConversationSummaryResponse])
def list_conversations(team_id: str):
    return conv_svc.list_conversations(team_id)
