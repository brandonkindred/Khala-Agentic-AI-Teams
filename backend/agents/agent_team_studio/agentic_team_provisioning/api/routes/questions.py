"""Agentic team provisioning API — pending-question endpoints.

Handlers delegate to ``api.services.questions`` so business logic stays out of the router.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter

from agent_team_studio.agentic_team_provisioning.api.services import questions as questions_svc
from agent_team_studio.agentic_team_provisioning.models import (
    SubmitTeamAnswersRequest,
    TeamPendingQuestion,
)

router = APIRouter()


@router.get("/teams/{team_id}/questions", response_model=List[TeamPendingQuestion])
def list_team_questions(team_id: str):
    """See ``api.services.questions.list_team_questions`` for the full contract."""
    return questions_svc.list_team_questions(team_id)


@router.post("/teams/{team_id}/questions/{job_id}/answers")
def submit_team_answers(team_id: str, job_id: str, req: SubmitTeamAnswersRequest):
    """See ``api.services.questions.submit_team_answers`` for the full contract."""
    return questions_svc.submit_team_answers(team_id, job_id, req)
