"""Agentic team provisioning API — team job-status endpoints.

Handlers delegate to ``api.services.jobs`` so business logic stays out of the router.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter

from agent_team_studio.agentic_team_provisioning.api.services import jobs as jobs_svc
from agent_team_studio.agentic_team_provisioning.models import TeamJobDetail, TeamJobSummary

router = APIRouter()


@router.get("/teams/{team_id}/jobs", response_model=List[TeamJobSummary])
def list_team_jobs(team_id: str):
    """See ``api.services.jobs.list_team_jobs`` for the full contract."""
    return jobs_svc.list_team_jobs(team_id)


@router.get("/teams/{team_id}/jobs/{job_id}", response_model=TeamJobDetail)
def get_team_job(team_id: str, job_id: str):
    """See ``api.services.jobs.get_team_job`` for the full contract."""
    return jobs_svc.get_team_job(team_id, job_id)
