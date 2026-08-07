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
    return testing_svc.set_team_mode(team_id, req)


@router.post("/teams/{team_id}/test-chat/sessions", response_model=TestChatSession, status_code=201)
def create_test_chat_session(team_id: str, req: CreateTestChatSessionRequest):
    return testing_svc.create_test_chat_session(team_id, req)


@router.get("/teams/{team_id}/test-chat/sessions", response_model=List[TestChatSession])
def list_test_chat_sessions(team_id: str, agent_name: Optional[str] = None):
    return testing_svc.list_test_chat_sessions(team_id, agent_name=agent_name)


@router.get(
    "/teams/{team_id}/test-chat/sessions/{session_id}", response_model=TestChatSessionDetail
)
def get_test_chat_session(team_id: str, session_id: str):
    return testing_svc.get_test_chat_session(team_id, session_id)


@router.put("/teams/{team_id}/test-chat/sessions/{session_id}/name")
def rename_test_chat_session(team_id: str, session_id: str, req: RenameTestChatSessionRequest):
    return testing_svc.rename_test_chat_session(team_id, session_id, req)


@router.delete("/teams/{team_id}/test-chat/sessions/{session_id}", status_code=204)
def delete_test_chat_session(team_id: str, session_id: str):
    return testing_svc.delete_test_chat_session(team_id, session_id)


@router.post("/teams/{team_id}/test-chat/sessions/{session_id}/messages")
def send_test_chat_message(team_id: str, session_id: str, req: SendTestChatMessageRequest):
    return testing_svc.send_test_chat_message(team_id, session_id, req)


@router.get("/teams/{team_id}/test-chat/sessions/{session_id}/export")
def export_test_chat_session(team_id: str, session_id: str):
    return testing_svc.export_test_chat_session(team_id, session_id)


@router.put("/teams/{team_id}/test-chat/messages/{message_id}/rating")
def rate_test_chat_message(team_id: str, message_id: str, req: RateMessageRequest):
    return testing_svc.rate_test_chat_message(team_id, message_id, req)


@router.get("/teams/{team_id}/test-chat/quality-scores", response_model=List[AgentQualityScore])
def get_agent_quality_scores(team_id: str):
    return testing_svc.get_agent_quality_scores(team_id)


@router.post("/teams/{team_id}/test-pipeline/runs", response_model=TestPipelineRun, status_code=201)
def start_pipeline_run(team_id: str, req: StartPipelineRunRequest):
    return testing_svc.start_pipeline_run(team_id, req)


@router.get("/teams/{team_id}/test-pipeline/runs", response_model=List[TestPipelineRun])
def list_pipeline_runs(team_id: str):
    return testing_svc.list_pipeline_runs(team_id)


@router.get("/teams/{team_id}/test-pipeline/runs/{run_id}", response_model=TestPipelineRun)
def get_pipeline_run(team_id: str, run_id: str):
    return testing_svc.get_pipeline_run(team_id, run_id)


@router.post("/teams/{team_id}/test-pipeline/runs/{run_id}/input")
def submit_pipeline_input(team_id: str, run_id: str, req: SubmitPipelineInputRequest):
    return testing_svc.submit_pipeline_input(team_id, run_id, req)


@router.post("/teams/{team_id}/test-pipeline/runs/{run_id}/cancel")
def cancel_pipeline_run(team_id: str, run_id: str):
    return testing_svc.cancel_pipeline_run(team_id, run_id)
