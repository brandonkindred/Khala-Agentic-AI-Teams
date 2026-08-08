"""Agentic team provisioning API — testing mode, test-chat, and test-pipeline endpoints."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter

from agent_team_studio.agentic_team_provisioning.api.services import testing as testing_svc
from agent_team_studio.agentic_team_provisioning.models import (
    AgentQualityScore,
    CreateTestChatSessionRequest,
    RateMessageRequest,
    RenameTestChatSessionRequest,
    SendTestChatMessageRequest,
    SetTeamModeRequest,
    StartPipelineRunRequest,
    SubmitPipelineInputRequest,
    TestChatSession,
    TestChatSessionDetail,
    TestPipelineRun,
)

router = APIRouter()


@router.put("/teams/{team_id}/mode")
def set_team_mode(team_id: str, req: SetTeamModeRequest):
    """Toggle team between development and testing mode.

    Preconditions: ``team_id`` is a non-empty string; ``req.mode`` is a valid
        ``TeamMode``.
    Postconditions: ``200`` with ``{"team_id": ..., "mode": req.mode.value}``;
        ``404`` if the team is not found (mode unchanged).
    Invariants: the stored mode is always one of ``TeamMode``'s values; setting
        it never mutates the team's roster or processes.
    """
    return testing_svc.set_team_mode(team_id, req)


@router.post("/teams/{team_id}/test-chat/sessions", response_model=TestChatSession, status_code=201)
def create_test_chat_session(team_id: str, req: CreateTestChatSessionRequest):
    """Create a new chat test session for an agent.

    Preconditions: ``team_id`` is a non-empty string; ``req.agent_name`` names
        an agent on the team's roster.
    Postconditions: ``201`` with the new ``TestChatSession`` (a fresh session
        id, no messages yet); ``404`` if the team is not found, or if
        ``req.agent_name`` is not on the team's roster (no session created).
    """
    return testing_svc.create_test_chat_session(team_id, req)


@router.get("/teams/{team_id}/test-chat/sessions", response_model=List[TestChatSession])
def list_test_chat_sessions(team_id: str, agent_name: Optional[str] = None):
    """List chat test sessions for a team, optionally filtered by agent.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: ``200`` with a ``TestChatSession`` for each session
        belonging to ``team_id`` (filtered to ``agent_name`` when given, empty
        list if none match); ``404`` if ``team_id`` is unknown.
    """
    return testing_svc.list_test_chat_sessions(team_id, agent_name=agent_name)


@router.get(
    "/teams/{team_id}/test-chat/sessions/{session_id}", response_model=TestChatSessionDetail
)
def get_test_chat_session(team_id: str, session_id: str):
    """Get a chat session with full message history and suggested prompts.

    Preconditions: ``team_id`` and ``session_id`` are non-empty strings.
    Postconditions: ``200`` with the session, its messages, and starter
        prompts (only generated when the session has no messages yet, and
        left empty rather than failing the request if the session's agent
        isn't on the roster); ``404`` if the session doesn't exist or belongs
        to a different team.
    """
    return testing_svc.get_test_chat_session(team_id, session_id)


@router.put("/teams/{team_id}/test-chat/sessions/{session_id}/name")
def rename_test_chat_session(team_id: str, session_id: str, req: RenameTestChatSessionRequest):
    """Rename a chat test session.

    Preconditions: ``team_id`` and ``session_id`` are non-empty strings;
        ``req.session_name`` is the new display name.
    Postconditions: ``200`` with ``{"session_id": ..., "session_name": ...}``;
        the session's stored name is updated; ``404`` if the session doesn't
        exist or belongs to a different team (name unchanged).
    """
    return testing_svc.rename_test_chat_session(team_id, session_id, req)


@router.delete("/teams/{team_id}/test-chat/sessions/{session_id}", status_code=204)
def delete_test_chat_session(team_id: str, session_id: str):
    """Delete a chat test session and its messages.

    Preconditions: ``team_id`` and ``session_id`` are non-empty strings.
    Postconditions: ``204`` and the session plus its messages removed when the
        session belongs to ``team_id``; ``404`` when the session is unknown or
        owned by a different team (store unchanged).
    """
    return testing_svc.delete_test_chat_session(team_id, session_id)


@router.post("/teams/{team_id}/test-chat/sessions/{session_id}/messages")
def send_test_chat_message(team_id: str, session_id: str, req: SendTestChatMessageRequest):
    """Send a message to an agent and get a synchronous response.

    The full conversation history is sent to the agent for multi-turn context.

    Preconditions: ``team_id``/``session_id`` refer to an existing session;
        ``req.content`` is non-empty (enforced by the request model).
    Postconditions: ``200`` with the session and its full message list,
        including the new user/assistant turn; ``404`` if the session is
        unknown or belongs to a different team; ``502`` if the agent
        invocation fails. The user and assistant messages are persisted
        together only after the agent call succeeds, so a failed invocation
        leaves no orphaned user message.
    """
    return testing_svc.send_test_chat_message(team_id, session_id, req)


@router.get("/teams/{team_id}/test-chat/sessions/{session_id}/export")
def export_test_chat_session(team_id: str, session_id: str):
    """Export a chat session transcript as Markdown text.

    Preconditions: ``team_id`` and ``session_id`` are non-empty strings.
    Postconditions: ``200`` with a Markdown attachment (``Content-Disposition``
        names the file ``{session_id}.md``) rendering every message in order,
        annotated with a checkmark/cross where a thumbs-up/down rating is
        present; ``404`` if the session doesn't exist or belongs to a
        different team.
    """
    return testing_svc.export_test_chat_session(team_id, session_id)


@router.put("/teams/{team_id}/test-chat/messages/{message_id}/rating")
def rate_test_chat_message(team_id: str, message_id: str, req: RateMessageRequest):
    """Rate an assistant message (thumbs up/thumbs down).

    Preconditions: ``team_id`` and ``message_id`` are non-empty strings;
        ``req.rating`` is a valid rating value.
    Postconditions: ``200`` with ``{"message_id": ..., "rating": req.rating.value}``;
        the message's stored rating is updated; ``404`` if no message with
        ``message_id`` exists under ``team_id`` (rating unchanged).
    """
    return testing_svc.rate_test_chat_message(team_id, message_id, req)


@router.get("/teams/{team_id}/test-chat/quality-scores", response_model=List[AgentQualityScore])
def get_agent_quality_scores(team_id: str):
    """Get aggregated quality scores per agent based on chat ratings.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: ``200`` with an ``AgentQualityScore`` per agent that has
        rated messages (empty list if none); ``404`` if the team is not found.
    """
    return testing_svc.get_agent_quality_scores(team_id)


@router.post("/teams/{team_id}/test-pipeline/runs", response_model=TestPipelineRun, status_code=201)
def start_pipeline_run(team_id: str, req: StartPipelineRunRequest):
    """Start an end-to-end pipeline test run.

    Preconditions: ``team_id`` is a non-empty string; ``req.process_id`` names
        a process belonging to the team.
    Postconditions: ``201`` with the created ``TestPipelineRun``; ``404`` if
        the team or ``req.process_id`` is not found (no run created); ``500``
        if dispatch fails after the run row was already created — see
        Invariants.
    Invariants: the run row is always created (and its ``temporal_owned`` flag
        persisted) before dispatch is attempted, so a dispatch failure marks
        that row FAILED rather than losing the run entirely; the ``500``
        response signals a failure to *start* the run, not evidence that no
        run row exists.
    """
    return testing_svc.start_pipeline_run(team_id, req)


@router.get("/teams/{team_id}/test-pipeline/runs", response_model=List[TestPipelineRun])
def list_pipeline_runs(team_id: str):
    """List pipeline test runs for a team.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: ``200`` with a ``TestPipelineRun`` per run recorded for the
        team (empty list if none); ``404`` if the team is not found.
    """
    return testing_svc.list_pipeline_runs(team_id)


@router.get("/teams/{team_id}/test-pipeline/runs/{run_id}", response_model=TestPipelineRun)
def get_pipeline_run(team_id: str, run_id: str):
    """Get the current status and step results of a pipeline test run.

    Preconditions: ``team_id`` and ``run_id`` are non-empty strings.
    Postconditions: ``200`` with the ``TestPipelineRun``; ``404`` if no run
        with ``run_id`` exists under ``team_id``.
    """
    return testing_svc.get_pipeline_run(team_id, run_id)


@router.post("/teams/{team_id}/test-pipeline/runs/{run_id}/input")
def submit_pipeline_input(team_id: str, run_id: str, req: SubmitPipelineInputRequest):
    """Submit human input at a WAIT step to resume the pipeline.

    Preconditions: ``team_id`` and ``run_id`` are non-empty strings;
        ``req.input`` is the value to resume with.
    Postconditions: ``200`` with the updated ``TestPipelineRun``; ``404`` if no
        run with ``run_id`` exists under ``team_id``; ``400`` if the run isn't
        ``waiting_for_input``; ``409`` if the run is no longer resumable
        (timed out, cancelled, or reaped) — a Temporal-owned run's workflow
        signal failure is logged rather than raised, since the resume is
        already durably recorded and reconciled at the WAIT timeout; a
        thread-owned run resumes the in-process runner directly.
    Invariants: whether a run is Temporal-owned or thread-owned is decided
        once, at creation, and read consistently here (``is_pipeline_run_temporal_owned``)
        — the branch taken never diverges from the run's persisted flag.
    """
    return testing_svc.submit_pipeline_input(team_id, run_id, req)


@router.post("/teams/{team_id}/test-pipeline/runs/{run_id}/cancel")
def cancel_pipeline_run(team_id: str, run_id: str):
    """Cancel a running or waiting pipeline test run.

    Preconditions: ``team_id`` and ``run_id`` are non-empty strings.
    Postconditions: ``200`` with the updated ``TestPipelineRun`` (cancelled
        status); ``404`` if no run with ``run_id`` exists under ``team_id``;
        ``400`` if the run isn't in a cancellable state (``running`` or
        ``waiting_for_input``); a Temporal-owned run's workflow cancellation
        is best-effort (failure logged, not raised, since the store is
        already updated), a thread-owned run's in-process runner is cancelled
        directly.
    Invariants: whether a run is Temporal-owned or thread-owned is decided
        once, at creation, and read consistently here
        (``is_pipeline_run_temporal_owned``) — the branch taken never diverges
        from the run's persisted flag.
    """
    return testing_svc.cancel_pipeline_run(team_id, run_id)
