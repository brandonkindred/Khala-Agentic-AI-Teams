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
    """Toggle a team between development and interactive testing mode.

    Thin delegate: all preconditions, postconditions, and error behavior are
    documented on ``api.services.testing.set_team_mode``, which this calls
    directly with no logic of its own.
    """
    return testing_svc.set_team_mode(team_id, req)


@router.post("/teams/{team_id}/test-chat/sessions", response_model=TestChatSession, status_code=201)
def create_test_chat_session(team_id: str, req: CreateTestChatSessionRequest):
    """Create a new test-chat session for an agent on the team's roster.

    Thin delegate: all preconditions, postconditions, and error behavior are
    documented on ``api.services.testing.create_test_chat_session``, which
    this calls directly with no logic of its own.
    """
    return testing_svc.create_test_chat_session(team_id, req)


@router.get("/teams/{team_id}/test-chat/sessions", response_model=List[TestChatSession])
def list_test_chat_sessions(team_id: str, agent_name: Optional[str] = None):
    """List a team's test-chat sessions, optionally filtered by agent.

    Thin delegate: all preconditions, postconditions, and error behavior are
    documented on ``api.services.testing.list_test_chat_sessions``, which
    this calls directly with no logic of its own.
    """
    return testing_svc.list_test_chat_sessions(team_id, agent_name=agent_name)


@router.get(
    "/teams/{team_id}/test-chat/sessions/{session_id}", response_model=TestChatSessionDetail
)
def get_test_chat_session(team_id: str, session_id: str):
    """Get a test-chat session with its message history and starter prompts.

    Thin delegate: all preconditions, postconditions, and error behavior are
    documented on ``api.services.testing.get_test_chat_session``, which this
    calls directly with no logic of its own.
    """
    return testing_svc.get_test_chat_session(team_id, session_id)


@router.put("/teams/{team_id}/test-chat/sessions/{session_id}/name")
def rename_test_chat_session(team_id: str, session_id: str, req: RenameTestChatSessionRequest):
    """Rename a test-chat session.

    Thin delegate: all preconditions, postconditions, and error behavior are
    documented on ``api.services.testing.rename_test_chat_session``, which
    this calls directly with no logic of its own.
    """
    return testing_svc.rename_test_chat_session(team_id, session_id, req)


@router.delete("/teams/{team_id}/test-chat/sessions/{session_id}", status_code=204)
def delete_test_chat_session(team_id: str, session_id: str):
    """Delete a test-chat session and its messages.

    Thin delegate: all preconditions, postconditions, and error behavior are
    documented on ``api.services.testing.delete_test_chat_session``, which
    this calls directly with no logic of its own.
    """
    return testing_svc.delete_test_chat_session(team_id, session_id)


@router.post("/teams/{team_id}/test-chat/sessions/{session_id}/messages")
def send_test_chat_message(team_id: str, session_id: str, req: SendTestChatMessageRequest):
    """Send a test-chat message and get the agent's synchronous reply.

    Thin delegate: all preconditions, postconditions, and error behavior are
    documented on ``api.services.testing.send_test_chat_message``, which
    this calls directly with no logic of its own.
    """
    return testing_svc.send_test_chat_message(team_id, session_id, req)


@router.get("/teams/{team_id}/test-chat/sessions/{session_id}/export")
def export_test_chat_session(team_id: str, session_id: str):
    """Export a test-chat session transcript as a Markdown attachment.

    Thin delegate: all preconditions, postconditions, and error behavior are
    documented on ``api.services.testing.export_test_chat_session``, which
    this calls directly with no logic of its own.
    """
    return testing_svc.export_test_chat_session(team_id, session_id)


@router.put("/teams/{team_id}/test-chat/messages/{message_id}/rating")
def rate_test_chat_message(team_id: str, message_id: str, req: RateMessageRequest):
    """Rate a test-chat assistant message (thumbs up/down).

    Thin delegate: all preconditions, postconditions, and error behavior are
    documented on ``api.services.testing.rate_test_chat_message``, which
    this calls directly with no logic of its own.
    """
    return testing_svc.rate_test_chat_message(team_id, message_id, req)


@router.get("/teams/{team_id}/test-chat/quality-scores", response_model=List[AgentQualityScore])
def get_agent_quality_scores(team_id: str):
    """Get aggregated per-agent quality scores from test-chat ratings.

    Thin delegate: all preconditions, postconditions, and error behavior are
    documented on ``api.services.testing.get_agent_quality_scores``, which
    this calls directly with no logic of its own.
    """
    return testing_svc.get_agent_quality_scores(team_id)


@router.post("/teams/{team_id}/test-pipeline/runs", response_model=TestPipelineRun, status_code=201)
def start_pipeline_run(team_id: str, req: StartPipelineRunRequest):
    """Start an end-to-end pipeline test run for one of the team's processes.

    Thin delegate: all preconditions, postconditions, and error behavior are
    documented on ``api.services.testing.start_pipeline_run``, which this
    calls directly with no logic of its own.
    """
    return testing_svc.start_pipeline_run(team_id, req)


@router.get("/teams/{team_id}/test-pipeline/runs", response_model=List[TestPipelineRun])
def list_pipeline_runs(team_id: str):
    """List pipeline test runs for a team.

    Thin delegate: all preconditions, postconditions, and error behavior are
    documented on ``api.services.testing.list_pipeline_runs``, which this
    calls directly with no logic of its own.
    """
    return testing_svc.list_pipeline_runs(team_id)


@router.get("/teams/{team_id}/test-pipeline/runs/{run_id}", response_model=TestPipelineRun)
def get_pipeline_run(team_id: str, run_id: str):
    """Get the current status and step results of a pipeline test run.

    Thin delegate: all preconditions, postconditions, and error behavior are
    documented on ``api.services.testing.get_pipeline_run``, which this
    calls directly with no logic of its own.
    """
    return testing_svc.get_pipeline_run(team_id, run_id)


@router.post("/teams/{team_id}/test-pipeline/runs/{run_id}/input")
def submit_pipeline_input(team_id: str, run_id: str, req: SubmitPipelineInputRequest):
    """Submit human input at a WAIT step to resume a pipeline test run.

    Thin delegate: all preconditions, postconditions, and error behavior are
    documented on ``api.services.testing.submit_pipeline_input``, which this
    calls directly with no logic of its own.
    """
    return testing_svc.submit_pipeline_input(team_id, run_id, req)


@router.post("/teams/{team_id}/test-pipeline/runs/{run_id}/cancel")
def cancel_pipeline_run(team_id: str, run_id: str):
    """Cancel a running or waiting pipeline test run.

    Thin delegate: all preconditions, postconditions, and error behavior are
    documented on ``api.services.testing.cancel_pipeline_run``, which this
    calls directly with no logic of its own.
    """
    return testing_svc.cancel_pipeline_run(team_id, run_id)
