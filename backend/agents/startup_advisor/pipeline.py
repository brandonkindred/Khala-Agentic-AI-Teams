"""Core advisor conversation pipeline: message processing + job-store bookkeeping.

Kept separate from ``api.main`` so the Temporal worker — which imports
``run_advisor_core`` via ``temporal.workflows.run_pipeline_activity`` — never
pulls in the FastAPI ``app`` object (and its side effects: DB pool
registration, middleware, route wiring) into the worker process. Mirrors
``road_trip_planning_team.pipeline``'s split for the same reason.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from startup_advisor.shared.job_store import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_RUNNING,
    is_job_cancelled,
    update_job,
)

WELCOME_MESSAGE = (
    "Welcome! I'm your startup advisor. I'm here to help you think through "
    "your startup strategy — from customer discovery to fundraising to execution.\n\n"
    "To give you the best advice, I'll need to understand your situation first. "
    "Let's start: what are you working on, and what stage is your startup at?"
)

DEFAULT_SUGGESTED = [
    "I'm validating a new idea and need help with customer discovery.",
    "I'm building an MVP and want to prioritize features.",
    "I need help with my go-to-market strategy.",
]


class ConversationMessageResponse(BaseModel):
    role: str
    content: str
    timestamp: str


class ArtifactResponse(BaseModel):
    artifact_id: int
    artifact_type: str
    title: str
    payload: dict[str, Any]
    created_at: str


class ConversationStateResponse(BaseModel):
    conversation_id: str
    messages: list[ConversationMessageResponse]
    context: dict[str, Any]
    artifacts: list[ArtifactResponse]
    suggested_questions: list[str]


def get_store():  # noqa: ANN202
    from startup_advisor.store import get_conversation_store

    return get_conversation_store()


def get_agent():  # noqa: ANN202
    from startup_advisor.assistant.agent import get_advisor_agent

    return get_advisor_agent()


def build_response(
    conversation_id: str,
    messages,  # noqa: ANN001
    context: dict[str, Any],
    artifacts,  # noqa: ANN001
    suggested_questions: list[str],
) -> ConversationStateResponse:
    return ConversationStateResponse(
        conversation_id=conversation_id,
        messages=[
            ConversationMessageResponse(role=m.role, content=m.content, timestamp=m.timestamp)
            for m in messages
        ],
        context=context,
        artifacts=[
            ArtifactResponse(
                artifact_id=a.artifact_id,
                artifact_type=a.artifact_type,
                title=a.title,
                payload=a.payload,
                created_at=a.created_at,
            )
            for a in artifacts
        ],
        suggested_questions=suggested_questions,
    )


def merge_context(existing: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Merge context_update into existing context, preserving prior values."""
    merged = dict(existing)
    for key, value in update.items():
        if value is not None and value != "":
            merged[key] = value
    return merged


def process_advisor_message(message: str) -> ConversationStateResponse:
    """Run one turn of the advisor conversation and return the updated state.

    Preconditions:
        - ``message`` is a non-empty user message.

    Postconditions:
        - The singleton conversation is created (with the welcome message) if
          it did not already exist.
        - ``message`` is appended as a "user" message, the agent's reply is
          appended as an "assistant" message, and any artifact the agent
          returns is persisted — all as side effects on the conversation
          store.
        - Returns a ``ConversationStateResponse`` reflecting the conversation
          state after these appends.
    """
    store = get_store()
    agent = get_agent()

    cid = store.get_or_create_singleton()
    state = store.get(cid)
    if state is None:
        raise RuntimeError("Failed to load conversation")
    messages, context = state

    if len(messages) == 0:
        store.append_message(cid, "assistant", WELCOME_MESSAGE)
        state = store.get(cid)
        if state is None:
            raise RuntimeError("Failed to load conversation")
        messages, context = state

    store.append_message(cid, "user", message)

    msg_pairs = [(m.role, m.content) for m in messages]
    msg_pairs.append(("user", message))

    reply, context_update, suggested_questions, artifact = agent.respond(
        msg_pairs, context, message
    )

    if context_update:
        context = merge_context(context, context_update)
        store.update_context(cid, context)

    store.append_message(cid, "assistant", reply)

    if artifact and isinstance(artifact, dict):
        artifact_type = artifact.get("type", "advice")
        title = artifact.get("title", "Untitled")
        content = artifact.get("content", artifact)
        store.add_artifact(cid, artifact_type, title, content)

    state = store.get(cid)
    if state is None:
        raise RuntimeError("Failed to reload conversation")
    messages, context = state
    artifacts = store.get_artifacts(cid)
    return build_response(cid, messages, context, artifacts, suggested_questions)


def run_advisor_core(job_id: str, message: str) -> None:
    """RUNNING/COMPLETED job-store bookkeeping around ``process_advisor_message``.

    Shared by the thread dispatch path (``api.main._run_advisor_message_background``)
    and the Temporal activity (``temporal.workflows.run_pipeline_activity``) so
    the state-machine transition lives in exactly one place.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.

    Postconditions:
        - If the job was cancelled before or during processing, returns
          without writing a terminal status (the cancellation already owns
          the job's terminal state). Note this check races with a concurrent
          cancel landing between the check and the ``update_job`` calls
          below — the job store has no atomic conditional update, so a
          cancellation in that window can be overwritten by a subsequent
          RUNNING/COMPLETED write. Accepted for this best-effort thread-mode
          cancellation; not a correctness issue for the Temporal path, which
          has its own cancellation semantics.
        - Otherwise marks the job RUNNING, runs ``process_advisor_message``,
          then marks it COMPLETED with the serialized result.
        - Propagates any exception unchanged — from ``process_advisor_message``
          or from either ``update_job`` call itself (e.g. a job-store
          connectivity failure) — the caller owns the failure policy.
    """
    if is_job_cancelled(job_id):
        return
    update_job(job_id, status=JOB_STATUS_RUNNING)
    result = process_advisor_message(message)
    if is_job_cancelled(job_id):
        return
    update_job(job_id, status=JOB_STATUS_COMPLETED, result=result.model_dump(mode="json"))
