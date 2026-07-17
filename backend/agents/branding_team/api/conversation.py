"""Conversation (chat) service helpers for the branding team API.

Holds the synchronous bodies behind the chat endpoints and the small helpers
they share (mission short-circuit, brand auto-creation, response assembly). The
async route wrappers in ``api.routes.conversations`` dispatch these onto the
bounded pipeline executor.

Collaborators tests monkeypatch (``orchestrator``, ``branding_store``,
``conversation_store``, ``assistant_agent`` via ``_get_assistant_agent``) are
owned by ``main`` and dereferenced through ``_main`` at call time; this module is
imported at the bottom of ``main`` so ``_main`` binds a fully-populated hub.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import HTTPException

from branding_team.api import main as _main
from branding_team.api.models import (
    ConversationMessage,
    ConversationStateResponse,
    CreateConversationRequest,
    SendMessageRequest,
)
from branding_team.api.state import _mission_has_brand_name, _mission_has_minimal_required_fields
from branding_team.assistant.store import _default_mission, _StoredMessage
from branding_team.models import BrandingMission, HumanReview, TeamOutput

logger = logging.getLogger(__name__)


def _run_orchestrator_if_ready(
    mission: BrandingMission,
    previous_mission: Optional[BrandingMission] = None,
    previous_output: Optional[TeamOutput] = None,
) -> Optional[TeamOutput]:
    """Run the pipeline for *mission*, or reuse a cached result.

    Returns None when the mission lacks the minimal required fields. The
    pipeline output is a pure function of the mission, so when the mission is
    unchanged since the previous run we return ``previous_output`` instead of
    re-running ~40 agents — the common case on the chat path, where most turns
    don't change the mission. Equality is a structural Pydantic compare; no
    serialization needed.
    """
    if not _mission_has_minimal_required_fields(mission):
        return None
    # NOTE: the short-circuit relies on BrandingMission being treated as
    # immutable — missions are replaced (model_copy/new instance), never mutated
    # in place. If that ever changes, this structural equality could match a
    # mutated-but-same-identity mission and serve stale output; compare a version
    # or content hash instead.
    if previous_output is not None and previous_mission == mission:
        return previous_output
    return _main.orchestrator.run(
        mission=mission,
        human_review=HumanReview(approved=False, feedback="Building brand from conversation."),
    )


def _brand_exists(brand_id: str) -> bool:
    return _main.branding_store.brand_exists(brand_id)


def _local_message(role: str, content: str) -> _StoredMessage:
    """Build an in-memory message mirroring what ``append_message`` just wrote,
    so a turn's response can be assembled without re-reading the row.

    Preconditions:
        ``role`` is ``"user"`` or ``"assistant"``; ``content`` is the message
        text that was just persisted for this conversation.
    Postconditions:
        Returns a ``_StoredMessage`` with the given role/content and an
        ISO-8601 UTC timestamp captured now (within sub-millisecond of the
        persisted row's timestamp, which is also app-clock generated).
    """
    return _StoredMessage(
        role=role,
        content=content,
        timestamp=datetime.now(tz=timezone.utc).isoformat(),
    )


def _conversation_to_response(
    conversation_id: str,
    brand_id: Optional[str],
    messages: list,
    mission: BrandingMission,
    latest_output: Optional[TeamOutput],
    suggested_questions: List[str],
) -> ConversationStateResponse:
    """Assemble the ``ConversationStateResponse`` API model from in-memory state.

    Preconditions:
        ``messages`` is a list of ``_StoredMessage``-like objects (each with
        ``role``/``content``/``timestamp``); the rest are already-validated
        conversation fields.
    Postconditions:
        Returns a ``ConversationStateResponse`` with ``messages`` mapped 1:1 to
        ``ConversationMessage`` and ``suggested_questions`` defaulted to ``[]``.
    """
    msg_list = [
        ConversationMessage(role=m.role, content=m.content, timestamp=m.timestamp) for m in messages
    ]
    return ConversationStateResponse(
        conversation_id=conversation_id,
        brand_id=brand_id,
        messages=msg_list,
        mission=mission,
        latest_output=latest_output,
        suggested_questions=suggested_questions or [],
    )


def _create_branding_conversation_impl(
    req: CreateConversationRequest,
) -> ConversationStateResponse:
    """Synchronous body of :func:`create_branding_conversation` (see its docstring).

    Preconditions:
        ``req`` is a validated ``CreateConversationRequest``.
    Postconditions:
        Same as the endpoint; runs entirely with blocking calls and is meant to
        be dispatched via ``asyncio.to_thread``.
    """
    conversation_store = _main.conversation_store
    brand_id = (req.brand_id or "").strip() or None
    if brand_id:
        if not _brand_exists(brand_id):
            raise HTTPException(status_code=404, detail="Brand not found")

    # Conversations are created unattached; auto-create-brand logic in
    # send_message will attach them once the mission has enough info.
    conversation_id = conversation_store.create(brand_id=brand_id)
    initial_message = (req.initial_message or "").strip()
    suggested_questions: List[str] = []
    # Track the response messages in memory (a fresh conversation has none yet)
    # so we don't re-read the row we just wrote.
    messages: List[_StoredMessage] = []
    mission: BrandingMission = _default_mission()
    latest_output: Optional[TeamOutput] = None

    if initial_message:
        # Freshly created conversation: no prior history, mission is the default.
        # conversation_id was just minted above in this same synchronous call, so
        # append failing here (conversation vanished) isn't reachable in practice;
        # checked anyway for consistency with send_branding_conversation_message.
        if not conversation_store.append_message(conversation_id, "user", initial_message):
            raise HTTPException(status_code=404, detail="Conversation not found")
        messages.append(_local_message("user", initial_message))
        reply, updated_mission, suggested_questions = _main._get_assistant_agent().respond(
            [], _default_mission(), initial_message
        )
        conversation_store.update_mission(conversation_id, updated_mission)
        if not conversation_store.append_message(conversation_id, "assistant", reply):
            logger.warning("Assistant reply not persisted for conversation %s", conversation_id)
        messages.append(_local_message("assistant", reply))
        output = _run_orchestrator_if_ready(updated_mission)
        if output is not None:
            conversation_store.update_output(conversation_id, output)
        mission, latest_output = updated_mission, output

        # Auto-create a brand when the user provided enough info in the initial message.
        if not brand_id and not req.skip_save and _mission_has_brand_name(updated_mission):
            brand_id = _auto_create_brand_from_conversation(
                conversation_id, updated_mission, output
            )
    else:
        reply = (
            "Hi! I'm your branding lead. I'll guide you through our 5-phase brand development framework — "
            "starting with your Strategic Core. Let's begin: what's your company or product name?"
        )
        if not conversation_store.append_message(conversation_id, "assistant", reply):
            logger.warning("Greeting not persisted for conversation %s", conversation_id)
        messages.append(_local_message("assistant", reply))
        suggested_questions = [
            "What's your company name?",
            "Who is your target audience?",
            "What does your company do?",
        ]

    return _conversation_to_response(
        conversation_id, brand_id, messages, mission, latest_output, suggested_questions
    )


def _ensure_default_client() -> str:
    """Find or create a default workspace client; return client_id.

    The default client name is configurable via ``BRANDING_DEFAULT_CLIENT_NAME``
    (default ``"My brands"``) for multi-tenant deployments.

    Note:
        Find-or-create is not atomic: two concurrent first-time requests could
        each create a default client. This is benign for the single-user
        assistant flow (subsequent calls return ``list_clients(limit=1)[0]``)
        and client names are intentionally non-unique (a workspace can have
        several clients), so a unique constraint isn't the right fix. A
        dedicated default-workspace flag or app-level lock is a follow-up.
    """
    branding_store = _main.branding_store
    clients = branding_store.list_clients(limit=1)
    if clients:
        return clients[0].id
    name = os.environ.get("BRANDING_DEFAULT_CLIENT_NAME", "My brands")
    client = branding_store.create_client(name=name)
    return client.id


def _auto_create_brand_from_conversation(
    conversation_id: str,
    mission: BrandingMission,
    output: Optional[TeamOutput],
) -> Optional[str]:
    """Create a brand from an unattached conversation and link the two.

    Preconditions:
        ``conversation_id`` refers to an existing conversation that is not yet
        attached to a brand, and ``mission`` carries a real (non-placeholder)
        company name.
    Postconditions:
        On success the conversation is attached to the new brand, the brand
        records the conversation id, and any ``output`` is appended as the
        first version. Returns the new brand id, or None if creation failed.

    Note:
        The steps run as independent statements (each store call takes its own
        ``shared_postgres`` connection), so this sequence is NOT atomic: if a
        later step raises, the brand may already exist while the conversation
        link or first version is missing. Acceptable for the single-user
        assistant flow today; making it transactional requires cross-store
        connection sharing and is tracked as a follow-up.
    """
    branding_store = _main.branding_store
    conversation_store = _main.conversation_store
    client_id = _ensure_default_client()
    brand = branding_store.create_brand(
        client_id=client_id,
        mission=mission,
        name=mission.company_name,
    )
    if not brand:
        return None
    # The brand now exists. If any linkage step below fails, the brand is
    # orphaned (created but not attached). Log a warning that names the brand so
    # the inconsistency is recoverable, then re-raise — the steps are not atomic
    # (see the Note above), so we surface the failure rather than hide it.
    try:
        conversation_store.set_brand(conversation_id, brand.id)
        branding_store.update_brand(client_id, brand.id, conversation_id=conversation_id)
        if output:
            branding_store.append_brand_version(client_id, brand.id, output)
    except Exception:
        logger.warning(
            "Brand %s was created but linking it to conversation %s failed; "
            "the brand may be orphaned",
            brand.id,
            conversation_id,
            exc_info=True,
        )
        raise
    logger.info("Auto-created brand %s from conversation %s", brand.id, conversation_id)
    return brand.id


def _send_branding_conversation_message_impl(
    conversation_id: str, payload: SendMessageRequest
) -> ConversationStateResponse:
    """Synchronous body of :func:`send_branding_conversation_message`.

    Preconditions:
        ``conversation_id`` is a string; ``payload`` is a validated
        ``SendMessageRequest``.
    Postconditions:
        Same as the endpoint; runs entirely with blocking calls and is meant to
        be dispatched via ``asyncio.to_thread``.
    """
    conversation_store = _main.conversation_store
    state = conversation_store.get_state(conversation_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    brand_id = state.brand_id
    # If the write does not land (conversation no longer exists), don't go on to
    # build an in-memory response that claims the message was persisted.
    if not conversation_store.append_message(conversation_id, "user", payload.message):
        raise HTTPException(status_code=404, detail="Conversation not found")
    history_pairs = [(m.role, m.content) for m in state.messages]
    reply, updated_mission, suggested_questions = _main._get_assistant_agent().respond(
        history_pairs, state.mission, payload.message
    )
    conversation_store.update_mission(conversation_id, updated_mission)
    # The reply is already computed and returned to the caller; if this write
    # doesn't land (conversation vanished mid-turn) log it rather than fail the
    # response, so the inconsistency is at least visible in the logs.
    if not conversation_store.append_message(conversation_id, "assistant", reply):
        logger.warning("Assistant reply not persisted for conversation %s", conversation_id)
    # Reuse the prior output when the mission is unchanged this turn; the
    # short-circuit returns the same object, so identity tells us whether a
    # fresh run happened and thus whether a write is needed.
    output = _run_orchestrator_if_ready(updated_mission, state.mission, state.latest_output)
    if output is not None and output is not state.latest_output:
        conversation_store.update_output(conversation_id, output)

    # Auto-create a brand when the user has provided at least a company name and conversation is unattached.
    if not brand_id and not payload.skip_save and _mission_has_brand_name(updated_mission):
        brand_id = _auto_create_brand_from_conversation(conversation_id, updated_mission, output)

    # Assemble the response from known state instead of re-reading the row.
    messages = list(state.messages) + [
        _local_message("user", payload.message),
        _local_message("assistant", reply),
    ]
    latest_output = output if output is not None else state.latest_output
    return _conversation_to_response(
        conversation_id, brand_id, messages, updated_mission, latest_output, suggested_questions
    )
