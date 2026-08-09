"""Conversation domain logic for agentic team provisioning HTTP.

Preconditions: callers pass the same request models / ids the former ``main``
    handlers accepted.
Postconditions: behavior matches the pre-split handlers. Roster/process saves
    (via ``main._save_agents_and_process``) happen before either chat message
    of a turn is persisted, so a registry-outage 503 leaves the conversation
    history untouched instead of stranding a half-saved turn — see
    ``create_conversation``/``send_message`` for the exact failure-mode
    boundaries. Collaborators are read from ``api.main`` at call time.
"""

from __future__ import annotations

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
    """Create a conversation and, if given, run its first turn.

    When ``req.initial_message`` is given, the turn's writes (user + assistant
    messages, roster save, process save) happen in dependency order but are
    committed to the conversation history only after the LLM call and the
    roster/process saves have all succeeded. Without an initial message, this
    atomicity guarantee doesn't apply — there is no LLM call or roster/process
    save, just a single greeting message written directly.

    Preconditions: ``req.team_id`` names an existing team.
    Postconditions: on success, the conversation exists with the greeting or
        the full first turn persisted. When an initial message is given: a
        failure in ``_agent.respond`` (LLM error, propagates as-is) or
        ``_save_agents_and_process`` (roster/process save failure, raised as
        ``HTTPException(503)`` — see its docstring for the non-retryable-error
        exceptions to that) happens before either append, so no partial turn
        is left and a client retry re-processes a clean conversation. That
        guarantee narrows once the two ``_store.append_message`` calls begin:
        each is its own committed write, so a failure between them (e.g. a
        store outage on the second call) can leave the user message persisted
        without the assistant reply — callers should not assume every error
        from this route leaves the conversation untouched, only errors raised
        before the appends start.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    # Validate team exists
    team = _main._store.get_team(req.team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    conversation_id = _main._store.create_conversation(team_id=req.team_id)

    if req.initial_message:
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

        _main._save_agents_and_process(req.team_id, conversation_id, agents_data, process)

        # Persist the turn only now that the LLM call and roster/process saves
        # have succeeded — see the docstring's failure-mode postcondition.
        _main._store.append_message(conversation_id, "user", req.initial_message)
        _main._store.append_message(conversation_id, "assistant", reply)

        return _build_state_response(conversation_id, req.team_id, process, suggestions)

    # No initial message — just add the greeting
    _main._store.append_message(conversation_id, "assistant", _main.GREETING)
    return _build_state_response(conversation_id, req.team_id, None, _main.DEFAULT_SUGGESTIONS)


def send_message(conversation_id: str, req: SendMessageRequest):
    """Run one conversation turn and append it to the conversation history.

    Preconditions: ``conversation_id`` names an existing conversation —
        checked first, before any LLM call or store mutation: an unknown
        ``conversation_id`` raises ``HTTPException(404)`` immediately.
    Postconditions: on success, both the user message and the assistant
        reply are appended (in that order) and any updated process is saved.
        A failure in ``_agent.respond`` (LLM error, propagates as-is) or
        ``_save_agents_and_process`` (roster/process save failure, raised as
        ``HTTPException(503)``) happens before either append, so the
        conversation history stays exactly as it was and a client retry
        cannot duplicate or half-save a turn from *those* failures. That
        guarantee narrows once the two ``_store.append_message`` calls begin:
        each is its own committed write, so a failure between them (e.g. a
        store outage on the second call) can leave the user message persisted
        without the assistant reply — callers should not assume every error
        from this route leaves the conversation untouched, only errors raised
        before the appends start.
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

    reply, updated_process, suggestions, agents_data = _main._agent.respond(
        conversation_history=history,
        current_process=current_process,
        user_message=req.message,
        current_agents=existing_agents,
    )

    _main._save_agents_and_process(team_id, conversation_id, agents_data, updated_process)

    effective_process = updated_process or current_process

    # Persist the turn only now that the LLM call and roster/process saves
    # have succeeded — see the docstring's failure-mode postcondition.
    _main._store.append_message(conversation_id, "user", req.message)
    _main._store.append_message(conversation_id, "assistant", reply)

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
