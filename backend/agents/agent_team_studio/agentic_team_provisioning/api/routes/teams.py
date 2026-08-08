"""Agentic team provisioning API — teams CRUD + roster endpoints.

Handlers delegate to ``api.services.teams`` so business logic stays out of the router.
"""

from __future__ import annotations

from fastapi import APIRouter

from agent_team_studio.agentic_team_provisioning.api.services import teams as teams_svc
from agent_team_studio.agentic_team_provisioning.models import (
    AddAgentFromRegistryRequest,
    AgenticTeamAgent,
    CreateTeamRequest,
    CreateTeamResponse,
    GeneratedManifestsResponse,
    RosterValidationResult,
    TeamDetailResponse,
    TeamSummary,
    UpdateAgentRequest,
)

router = APIRouter()


@router.post("/teams", response_model=CreateTeamResponse)
def create_team(req: CreateTeamRequest):
    return teams_svc.create_team(req)


@router.get("/teams", response_model=list[TeamSummary])
def list_teams():
    """List every persisted agentic team.

    Preconditions: none.
    Postconditions: returns ``200`` with a ``TeamSummary`` for each team row,
        delegating directly to ``teams_svc.list_teams`` (store's default
        order); an empty list if no teams exist.
    """
    return teams_svc.list_teams()


@router.get("/teams/{team_id}", response_model=TeamDetailResponse)
def get_team(team_id: str):
    return teams_svc.get_team(team_id)


@router.get("/teams/{team_id}/agents", response_model=list[AgenticTeamAgent])
def list_team_agents(team_id: str):
    """Return the named agents pool (roster) for this team.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: returns ``200`` with the team's roster as a list of
        ``AgenticTeamAgent`` (empty if no agents have been added yet),
        delegating directly to ``teams_svc.list_team_agents``; ``404`` if
        the team is not found.
    """
    return teams_svc.list_team_agents(team_id)


@router.get("/teams/{team_id}/agents/manifests", response_model=GeneratedManifestsResponse)
def list_team_agent_manifests(team_id: str):
    return teams_svc.list_team_agent_manifests(team_id)


@router.get("/teams/{team_id}/roster/validation", response_model=RosterValidationResult)
def validate_team_roster(team_id: str):
    return teams_svc.validate_team_roster(team_id)


@router.post(
    "/teams/{team_id}/agents/from-registry", response_model=AgenticTeamAgent, status_code=201
)
def add_agent_from_registry(team_id: str, req: AddAgentFromRegistryRequest):
    return teams_svc.add_agent_from_registry(team_id, req)


@router.delete("/teams/{team_id}/agents/{agent_name:path}", status_code=204)
def remove_agent_from_roster(team_id: str, agent_name: str):
    return teams_svc.remove_agent_from_roster(team_id, agent_name)


@router.put("/teams/{team_id}/agents/{agent_name:path}", response_model=AgenticTeamAgent)
def update_roster_agent(team_id: str, agent_name: str, req: UpdateAgentRequest):
    return teams_svc.update_roster_agent(team_id, agent_name, req)
