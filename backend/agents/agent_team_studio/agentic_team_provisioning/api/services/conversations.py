"""Conversation domain logic for agentic team provisioning HTTP.

Preconditions: callers pass the same request models / ids the former ``main``
    handlers accepted.
Postconditions: behavior matches the pre-split handlers, including 503 on
    registry failure after a persisted chat turn. Collaborators are read from
    ``api.main`` at call time.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException

from agent_team_studio.agentic_team_provisioning.models import (
    ConversationStateResponse,
    ConversationSummaryResponse,
    CreateConversationRequest,
    ProcessDefinition,
    SendMessageRequest,
    SetConversationProcessRequest,
)

logger = logging.getLogger(__name__)


def _build_state_response(
    conversation_id: str,
    team_id: str,
    process: Optional[ProcessDefinition],
    suggested_questions: list[str],
) -> ConversationStateResponse:
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    messages = _main._store.get_messages(conversation_id)
    return ConversationStateResponse(
        conversation_id=conversation_id,
        team_id=team_id,
        messages=messages,
        current_process=process,
        suggested_questions=suggested_questions,
    )


def create_conversation(req: CreateConversationRequest):
    """Start a new conversation for a team, optionally seeded with an initial message.

    Preconditions: ``req.team_id`` refers to an existing team.
    Postconditions: ``200`` with the conversation's state; ``404`` if the team is
        unknown (nothing persisted). When ``req.initial_message`` is given, the
        user/assistant chat turn is appended to the store *before* the LLM's
        roster is saved via ``_save_agents_from_llm``; if that save fails because
        the agent registry is unavailable, this raises ``503`` instead of an
        opaque ``500`` — but the conversation is left with the chat turn already
        persisted and the roster/registry write rolled back (partial state). A
        client retrying the same message re-processes it against a conversation
        that already contains the prior turn.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    # Validate team exists
    team = _main._store.get_team(req.team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    conversation_id = _main._store.create_conversation(team_id=req.team_id)

    if req.initial_message:
        _main._store.append_message(conversation_id, "user", req.initial_message)

        existing_agents = [
            {"agent_name": a.agent_name, "role": a.role}
            for a in _main._store.list_team_agents(req.team_id)
        ] or None

        reply, process, suggestions, agents_data = _main._agent.respond(
            conversation_history=[],
            current_process=None,
            user_message=req.initial_message,
            current_agents=existing_agents,
        )

        _main._store.append_message(conversation_id, "assistant", reply)
        try:
            _main._save_agents_from_llm(req.team_id, agents_data)
        except Exception as e:
            logger.warning(
                "Roster save failed for team %s after conversation %s turn: %s",
                req.team_id,
                conversation_id,
                e,
            )
            raise HTTPException(status_code=503, detail="Agent registry unavailable") from e
        if process:
            _main._store.save_process(req.team_id, process)
            _main._store.set_conversation_process(conversation_id, process.process_id)
            _main._after_process_saved(req.team_id, process)

        return _build_state_response(conversation_id, req.team_id, process, suggestions)

    # No initial message — just add the greeting
    _main._store.append_message(conversation_id, "assistant", _main.GREETING)
    return _build_state_response(conversation_id, req.team_id, None, _main.DEFAULT_SUGGESTIONS)


def send_message(conversation_id: str, req: SendMessageRequest):
    """Send a user message on an existing conversation and get the assistant's reply.

    Preconditions: ``conversation_id`` refers to an existing conversation.
    Postconditions: ``200`` with the conversation's updated state; ``404`` if the
        conversation is unknown (nothing persisted). The user/assistant chat turn
        is appended to the store *before* the LLM's roster is saved via
        ``_save_agents_from_llm``; if that save fails because the agent registry
        is unavailable, this raises ``503`` instead of an opaque ``500`` — but the
        conversation is left with the chat turn already persisted and the
        roster/registry write rolled back (partial state). A client retrying the
        same message re-processes it against a conversation that already contains
        the prior turn.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    team_id = _main._store.get_conversation_team_id(conversation_id)
    if not team_id:
        raise HTTPException(status_code=404, detail="Conversation not found")

    process_id = _main._store.get_conversation_process_id(conversation_id)
    current_process = _main._store.get_process(process_id) if process_id else None

    existing_agents = [
        {"agent_name": a.agent_name, "role": a.role} for a in _main._store.list_team_agents(team_id)
    ] or None

    existing_messages = _main._store.get_messages(conversation_id)
    history = [(m.role, m.content) for m in existing_messages]

    _main._store.append_message(conversation_id, "user", req.message)

    reply, updated_process, suggestions, agents_data = _main._agent.respond(
        conversation_history=history,
        current_process=current_process,
        user_message=req.message,
        current_agents=existing_agents,
    )

    _main._store.append_message(conversation_id, "assistant", reply)
    try:
        _main._save_agents_from_llm(team_id, agents_data)
    except Exception as e:
        logger.warning(
            "Roster save failed for team %s after conversation %s turn: %s",
            team_id,
            conversation_id,
            e,
        )
        raise HTTPException(status_code=503, detail="Agent registry unavailable") from e

    effective_process = current_process
    if updated_process:
        _main._store.save_process(team_id, updated_process)
        _main._store.set_conversation_process(conversation_id, updated_process.process_id)
        effective_process = updated_process
        _main._after_process_saved(team_id, updated_process)

    return _build_state_response(conversation_id, team_id, effective_process, suggestions)


def set_conversation_process(conversation_id: str, req: SetConversationProcessRequest):
    """Link a process to the active conversation so chat stays in sync with the visual editor.

    Preconditions: ``req.process_id`` is non-empty (enforced by the request model;
        a missing/blank value is a ``422`` before this handler runs).
    Postconditions: ``200`` with the linked ``conversation_id``/``process_id`` pair;
        ``404`` if the conversation or the process is unknown (link unchanged); ``403``
        if the process belongs to a different team than the conversation (link
        unchanged) — a conversation may only be linked to its own team's processes.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    team_id = _main._store.get_conversation_team_id(conversation_id)
    if not team_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    process = _main._store.get_process(req.process_id)
    if not process:
        raise HTTPException(status_code=404, detail="Process not found")
    if _main._store.get_process_team_id(req.process_id) != team_id:
        raise HTTPException(
            status_code=403, detail="Process does not belong to this conversation's team"
        )
    _main._store.set_conversation_process(conversation_id, req.process_id)
    return {"conversation_id": conversation_id, "process_id": req.process_id}


def get_conversation(conversation_id: str):
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    team_id = _main._store.get_conversation_team_id(conversation_id)
    if not team_id:
        raise HTTPException(status_code=404, detail="Conversation not found")

    process_id = _main._store.get_conversation_process_id(conversation_id)
    process = _main._store.get_process(process_id) if process_id else None

    return _build_state_response(conversation_id, team_id, process, [])


def list_conversations(team_id: str):
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    team = _main._store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    rows = _main._store.list_conversations(team_id)
    return [ConversationSummaryResponse(**r) for r in rows]
