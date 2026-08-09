"""Process CRUD domain logic for agentic team provisioning HTTP.

Preconditions: callers pass the same request models / ids the former ``main``
    handlers accepted.
Postconditions: behavior matches the pre-split handlers (status codes, bodies,
    background-provisioning side effects). Collaborators are read from
    ``api.main`` at call time so tests can ``monkeypatch.setattr(main, …)``.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException

from agent_team_studio.agentic_team_provisioning.models import (
    AgentEnvProvisionSummary,
    ProcessDefinition,
    ProcessOutput,
    ProcessStatus,
    ProcessTrigger,
    RecommendAgentsResponse,
    RecommendedAgent,
)


def list_processes(team_id: str):
    """List all processes defined for a team.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: ``200`` with the team's processes as a list of
        ``ProcessDefinition`` (empty if none have been created yet); ``404``
        if the team is not found.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    team = _main._store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return team.processes


def get_process(process_id: str):
    """Retrieve a single process definition by id.

    Note: unlike the team-scoped routes, this one is looked up globally by
    ``process_id`` alone (no ``team_id`` in the path) — the visual editor and
    conversation flows address a process directly once they know its id.

    Preconditions: ``process_id`` is a non-empty string.
    Postconditions: ``200`` with the ``ProcessDefinition``; ``404`` if no
        process with that id exists.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    process = _main._store.get_process(process_id)
    if not process:
        raise HTTPException(status_code=404, detail="Process not found")
    return process


def create_process(team_id: str):
    """Create a new blank process for the team.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: ``201`` with a fresh ``ProcessDefinition`` (a new UUID
        ``process_id``, name "New Process", no steps, ``status=DRAFT``)
        persisted under the team; ``404`` if the team is not found (no process
        created). Side effect: inserts a new process row via
        ``_store.save_process``.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    team = _main._store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    process = ProcessDefinition(
        process_id=str(uuid.uuid4()),
        name="New Process",
        description="",
        trigger=ProcessTrigger(),
        steps=[],
        output=ProcessOutput(),
        status=ProcessStatus.DRAFT,
    )
    _main._store.save_process(team_id, process)
    return process


def update_process(process_id: str, process: ProcessDefinition):
    """Update a process definition (visual editor saves).

    Preconditions: ``process_id`` is a non-empty string identifying an
        existing process; ``process.process_id`` must equal ``process_id``.
    Postconditions: ``200`` with the saved ``ProcessDefinition`` (the full
        body replaces the stored definition — this is a whole-document save,
        not a partial patch); ``404`` if the process (or its owning team) is
        not found; ``400`` if ``process.process_id`` doesn't match the URL
        (process unchanged in both error cases). Side effect: calls
        ``_after_process_saved``, which schedules background provisioning of
        per-step agent environments (``schedule_provision_step_agents``) for
        the updated process — this runs even for a no-op-looking save.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    existing = _main._store.get_process(process_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Process not found")
    if process.process_id != process_id:
        raise HTTPException(status_code=400, detail="process_id in body must match URL")
    team_id = _main._store.get_process_team_id(process_id)
    if not team_id:
        raise HTTPException(status_code=404, detail="Process team not found")
    _main._store.save_process(team_id, process)
    _main._after_process_saved(team_id, process)
    return process


def recommend_agents_for_step(process_id: str, step_id: str):
    """Recommend roster agents for a specific process step based on its description.

    Scoring is a simple token-overlap heuristic, not semantic matching:
    lowercased words (length > 2) from the step's ``name``/``description`` are
    intersected against each roster agent's combined
    skills/capabilities/tools/expertise; the overlap *count* is the
    ``match_score``. Agents with zero overlap are omitted entirely, and the
    remaining ones are sorted by descending score and capped to the top 10.

    Preconditions: ``process_id`` and ``step_id`` are non-empty strings.
    Postconditions: ``200`` with a ``RecommendAgentsResponse`` (``recommended_agents``
        is empty when the process's team has no matching agents, or is
        unresolvable); ``404`` if the process is unknown, or the process has
        no step with ``step_id``.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    process = _main._store.get_process(process_id)
    if not process:
        raise HTTPException(status_code=404, detail="Process not found")
    step = next((s for s in process.steps if s.step_id == step_id), None)
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")

    team_id = _main._store.get_process_team_id(process_id)
    recommendations: list[RecommendedAgent] = []

    if team_id:
        team = _main._store.get_team(team_id)
        if team:
            search_tokens = {
                t.lower() for t in f"{step.name} {step.description}".split() if len(t) > 2
            }
            for agent in team.agents:
                agent_tokens = {
                    t.lower()
                    for t in (agent.skills + agent.capabilities + agent.tools + agent.expertise)
                }
                overlap = len(search_tokens & agent_tokens)
                if overlap > 0:
                    recommendations.append(
                        RecommendedAgent(
                            agent_name=agent.agent_name,
                            source="roster",
                            role=agent.role,
                            skills=agent.skills,
                            tools=agent.tools,
                            match_score=float(overlap),
                        )
                    )

    recommendations.sort(key=lambda r: -r.match_score)

    return RecommendAgentsResponse(
        step_id=step_id,
        step_name=step.name,
        recommended_agents=recommendations[:10],
    )


def list_team_agent_environments(team_id: str):
    """Per-step agent provisioning status (Agent Provisioning team / sandboxed envs).

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: ``200`` with an ``AgentEnvProvisionSummary`` per recorded
        provisioning attempt for the team (empty if none have run yet); ``404``
        if the team is not found.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    team = _main._store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    rows = _main._store.list_agent_env_provisions(team_id)
    return [AgentEnvProvisionSummary(**r) for r in rows]
