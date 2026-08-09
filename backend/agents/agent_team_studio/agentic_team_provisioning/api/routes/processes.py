"""Agentic team provisioning API — process CRUD endpoints.

Handlers delegate to ``api.services.processes`` so business logic stays out of the router.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter

from agent_team_studio.agentic_team_provisioning.api.services import processes as processes_svc
from agent_team_studio.agentic_team_provisioning.models import (
    AgentEnvProvisionSummary,
    ProcessDefinition,
    RecommendAgentsResponse,
)

router = APIRouter()


@router.get("/teams/{team_id}/processes", response_model=list[ProcessDefinition])
def list_processes(team_id: str):
    """See ``api.services.processes.list_processes`` for the full contract."""
    return processes_svc.list_processes(team_id)


@router.get("/processes/{process_id}", response_model=ProcessDefinition)
def get_process(process_id: str):
    """See ``api.services.processes.get_process`` for the full contract."""
    return processes_svc.get_process(process_id)


@router.post("/teams/{team_id}/processes", response_model=ProcessDefinition, status_code=201)
def create_process(team_id: str):
    """See ``api.services.processes.create_process`` for the full contract."""
    return processes_svc.create_process(team_id)


@router.put("/processes/{process_id}", response_model=ProcessDefinition)
def update_process(process_id: str, process: ProcessDefinition):
    """See ``api.services.processes.update_process`` for the full contract."""
    return processes_svc.update_process(process_id, process)


@router.post(
    "/processes/{process_id}/steps/{step_id}/recommend-agents",
    response_model=RecommendAgentsResponse,
)
def recommend_agents_for_step(process_id: str, step_id: str):
    """See ``api.services.processes.recommend_agents_for_step`` for the full contract."""
    return processes_svc.recommend_agents_for_step(process_id, step_id)


@router.get("/teams/{team_id}/agent-environments", response_model=List[AgentEnvProvisionSummary])
def list_team_agent_environments(team_id: str):
    """See ``api.services.processes.list_team_agent_environments`` for the full contract."""
    return processes_svc.list_team_agent_environments(team_id)
