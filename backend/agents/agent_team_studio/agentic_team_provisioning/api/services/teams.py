"""Team CRUD and roster domain logic for agentic team provisioning HTTP.

Preconditions: callers pass the same request models / ids the former ``main``
    handlers accepted.
Postconditions: behavior matches the pre-split handlers (status codes, bodies,
    registry side effects). Collaborators are read from ``api.main`` at call
    time so tests can ``monkeypatch.setattr(main, …)``.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from fastapi import HTTPException, Response
from pydantic import ValidationError

from agent_registry.models import AgentManifest
from agent_team_studio.agentic_team_provisioning.manifest_generation import (
    build_agent_manifest,
    is_generated_manifest,
)
from agent_team_studio.agentic_team_provisioning.models import (
    SOURCE_GENERATED,
    SOURCE_REGISTRY,
    AddAgentFromRegistryRequest,
    AgenticTeamAgent,
    CreateTeamRequest,
    CreateTeamResponse,
    GeneratedManifestsResponse,
    TeamDetailResponse,
    TeamSummary,
    UpdateAgentRequest,
)

logger = logging.getLogger(__name__)


def create_team(req: CreateTeamRequest):
    """Create a new agentic team and provision its infrastructure.

    Persists the team row first, then provisions the team's infrastructure.
    If provisioning fails, the team row is rolled back via ``_store.delete_team``
    so no orphaned, infrastructure-less row survives a failed create.

    Preconditions: ``req.name`` is a non-empty team name (enforced by
        ``CreateTeamRequest``).
    Postconditions: on success, the team row exists, infrastructure is
        provisioned, and ``200`` is returned with the created team. On
        provisioning failure, the team row is removed (best-effort — a
        rollback failure is logged but never masks the ``500``) and an
        ``HTTPException(500)`` is raised.
    Invariants: when provisioning fails and the compensating delete succeeds,
        no team row remains in Postgres without corresponding infrastructure.
        The delete is best-effort, so a rollback failure is logged but the row
        may remain — see Postconditions.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    team = _main._store.create_team(name=req.name, description=req.description)
    try:
        _main.provision_team(team.team_id)
    except Exception as exc:
        logger.exception(
            "Failed to provision infrastructure for team %s; rolling back team row",
            team.team_id,
        )
        try:
            _main._store.delete_team(team.team_id)
        except Exception:
            # Best-effort: a rollback failure must not swallow the 500 below
            # or hide the original provisioning error from the caller.
            logger.exception(
                "Failed to roll back team row for team %s after provisioning failure",
                team.team_id,
            )
        raise HTTPException(
            status_code=500, detail="Failed to provision team infrastructure."
        ) from exc
    return CreateTeamResponse(
        team_id=team.team_id,
        name=team.name,
        description=team.description,
        created_at=team.created_at,
    )


def list_teams():
    """List all agentic teams.

    Preconditions: none.
    Postconditions: ``200`` with a ``TeamSummary`` for every persisted team, in the
        store's default order; an empty list if no teams exist.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    rows = _main._store.list_teams()
    return [TeamSummary(**r) for r in rows]


def get_team(team_id: str):
    """Retrieve a single agentic team by id.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: ``200`` with the full ``TeamDetailResponse`` when the team
        exists; ``404`` if no team with the given id is found.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    team = _main._store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return TeamDetailResponse(team=team)


def list_team_agents(team_id: str):
    """Named agents pool (roster) for this team.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: ``200`` with the team's roster as a list of
        ``EnrichedRosterAgent`` (thin refs plus persona fields resolved from each
        agent's ``manifest_id``; empty if no agents have been added yet); ``404``
        if the team is not found.
    Invariants: none.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    team = _main._store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return [_main.enrich_roster_agent(a) for a in team.agents]


def list_team_agent_manifests(team_id: str):
    """Generated agent_registry manifests (with the cognition core stamped) for the roster.

    Each **generated** roster agent is rendered into a validated ``AgentManifest``
    carrying the batteries-included ``cognition`` block; nothing is written to the
    registry's manifest discovery paths. A **registry-source** agent (added via Agent
    Studio's from-registry endpoint) instead returns its *original* registry manifest
    so the advertised id is the one that actually resolves for the Agent Console /
    ``/api/agents/{id}/invoke``.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: ``200`` with one manifest per roster agent — a generated agent's
        stamped wrapper, or a registry-source agent's *original* registry manifest;
        a registry-source agent whose manifest no longer resolves in this process is
        **omitted** rather than advertised with a synthetic generated id this team
        never registered (which would 404 on invoke). ``404`` if the team is unknown.
    """
    # ``get_registry`` is imported inline (not at module top) so the test suite's
    # ``monkeypatch.setattr("agent_registry.get_registry", …)`` is resolved at call
    # time — a top-level ``from agent_registry import get_registry`` would bind the
    # name before the patch and bypass the fake registry.
    from agent_registry import get_registry
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    team = _main._store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    registry = get_registry()
    manifests = []
    for a in team.agents:
        if a.source == SOURCE_REGISTRY:
            # Advertise only the original, resolvable registry manifest; if it no
            # longer resolves here, omit the agent rather than fabricate an
            # unresolvable wrapper id.
            original = registry.get(a.manifest_id) if a.manifest_id else None
            if original is not None:
                manifests.append(original)
            continue
        existing = registry.get(a.manifest_id) if a.manifest_id else None
        if existing is not None:
            manifests.append(existing)
        else:
            manifests.append(build_agent_manifest(team_id, a.agent_name))
    return GeneratedManifestsResponse(team_id=team_id, manifests=manifests)


def validate_team_roster(team_id: str):
    """Validate whether the roster fully covers the team's process needs.

    Runs every process's step/agent requirements against the roster (each
    referenced agent must exist and be assigned), flags roster agents unused by
    any process, and flags agents whose profile is too sparse (missing most of
    skills/capabilities/tools/expertise) — see ``roster_validation.validate_roster``.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: ``200`` with a ``RosterValidationResult`` summarizing
        coverage gaps (empty ``gaps`` and ``is_fully_staffed=True`` when the
        roster fully covers every process); ``404`` if the team is not found.
    Invariants: none — a read-only check, no roster or process state changes.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main
    from agent_team_studio.agentic_team_provisioning.roster_validation import validate_roster

    team = _main._store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return validate_roster(team)


def _roster_agent_from_manifest(manifest: AgentManifest) -> AgenticTeamAgent:
    """Project a registry ``AgentManifest`` into a thin roster ref (Agent Studio §5.3).

    Preconditions: ``manifest`` is a registered manifest with non-empty ``id`` and
        ``name``.
    Postconditions: returns an ``AgenticTeamAgent`` with only ``agent_name``,
        ``source == "registry"``, and ``manifest_id == manifest.id``. Persona
        fields are resolved at read time via ``enrich_roster_agent`` /
        ``resolve_persona``.
    """
    # Enforce the precondition with explicit validation rather than ``assert`` (which
    # ``python -O`` strips): ``AgentManifest.id``/``name`` are required but not
    # length-constrained, so an empty string passes Pydantic — fail fast here rather
    # than project a malformed manifest into the roster.
    if not (manifest.name and manifest.name.strip()):
        raise ValueError("manifest.name must be non-empty")
    if not manifest.id:
        raise ValueError("manifest.id must be set")
    return AgenticTeamAgent(
        agent_name=manifest.name,
        source=SOURCE_REGISTRY,
        manifest_id=manifest.id,
    )


def _unregister_generated_manifest(team_id: str, agent: AgenticTeamAgent) -> None:
    """Best-effort: drop a generated agent's stale in-process manifest from the registry.

    Called (as an ``on_replaced`` / ``on_deleted`` hook) under the store's team-row
    lock when a *generated* roster agent is removed or replaced, so the Agent Console
    catalog / ``/api/agents/{id}/invoke`` route stop resolving it. Running under the
    lock serializes it with the chat-save register, closing the re-add/replace races.

    Preconditions: ``agent.source == SOURCE_GENERATED`` (caller checks); invoked
        within the locked roster mutation that removes/replaces the row.
    Postconditions: the agent's generated manifest is unregistered if present. A
        registry failure is logged, **never raised** — so this best-effort cleanup
        can neither 500 the request nor roll back the committed roster mutation.
        (Chat-save registration via ``register_team_manifests`` is fail-closed
        instead; cleanup hooks stay best-effort so a transient unregister blip
        cannot undo a successful delete/replace.)
    """
    try:
        # ``get_registry`` stays inline so tests' ``monkeypatch`` of
        # ``agent_registry.get_registry`` resolves at call time (see
        # ``list_team_agent_manifests``).
        from agent_registry import get_registry

        get_registry().unregister(agent.manifest_id)
    except Exception:
        logger.warning(
            "Failed to unregister stale generated manifest for agent %s in team %s",
            agent.agent_name,
            team_id,
            exc_info=True,
        )


def _generated_manifest_cleanup(team_id: str) -> Callable[[Optional[AgenticTeamAgent]], None]:
    """Build the registry-cleanup hook shared by the add (``on_replaced``) and delete
    (``on_deleted``) routes.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: returns a callback that, run under the store's team lock with the
        row a roster mutation removed/replaced (``None`` for a plain insert),
        unregisters that row's stale manifest **iff** it was generated. Registry-source
        rows and ``None`` are left untouched. The callback is best-effort (non-raising).
    """

    def _cleanup(prior: Optional[AgenticTeamAgent]) -> None:
        if prior is not None and prior.source == SOURCE_GENERATED:
            _unregister_generated_manifest(team_id, prior)

    return _cleanup


def add_agent_from_registry(team_id: str, req: AddAgentFromRegistryRequest):
    """Add a registered agent to the team roster as a thin ref linked to its manifest.

    Re-adding the same manifest updates that roster entry in place. If this replaces a
    *generated* agent of the same name, that generated agent's stale in-process
    manifest is unregistered (the new entry is registry-source and resolves on its
    own) — the cleanup runs in the store's locked transaction against the row actually
    replaced, so it can't race a concurrent chat-save's register.

    Preconditions: ``req.manifest_id`` is non-empty (enforced by the request model).
    Postconditions: ``201`` with the thin roster ref persisted and an enriched response
        (persona joined from the manifest at read time);
        ``404`` if the team or the manifest id is unknown (roster unchanged); ``409``
        if the manifest is a *generated* roster agent (any team's — see below; roster
        unchanged); ``422`` if the resolved manifest is too malformed to project (e.g.
        blank name/id), so a bad registry entry surfaces as a client error rather than
        an unhandled 500; ``503`` if the registry lookup itself fails (e.g. the
        registry is unavailable), so an outage surfaces as a clear service-unavailable
        response rather than an opaque 500.
    """
    from agent_registry import get_registry
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    team = _main._store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    try:
        manifest = get_registry().get(req.manifest_id)
    except Exception as e:
        logger.warning("Registry lookup failed for manifest %s: %s", req.manifest_id, e)
        raise HTTPException(status_code=503, detail="Agent registry unavailable")
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent manifest: {req.manifest_id}")
    # Reject adding a *generated* roster manifest via the registry. Generated agents
    # are registered in-process (so they surface in the catalog) but are ephemeral and
    # roster-owned: ``register_team_manifests`` (un)registers them as their team's
    # roster changes. Projecting one back here creates a registry-source row whose
    # ``manifest_id`` the owning team can later unregister — leaving a roster entry
    # whose manifest no longer resolves for the catalog / invoke route. This is true
    # both for *this* team's own generated agent (already on the roster in generated
    # form with the same deterministic ``manifest_id``) AND for another team's
    # generated agent (it would dangle the moment that team drops the agent). Classify
    # on the ``"generated"`` tag — the same marker ``register_team_manifests`` uses — so
    # a hand-authored registry agent (real catalog entry, e.g. the same-name swap flow)
    # is unaffected.
    if is_generated_manifest(manifest):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Agent '{manifest.name}' is a generated team agent; generated agents "
                "are managed on their team's roster and cannot be added from the registry."
            ),
        )
    try:
        agent = _main._roster_agent_from_manifest(manifest)
    except (ValueError, ValidationError) as e:
        logger.warning(
            "Malformed registered manifest %s for team %s: %s", req.manifest_id, team_id, e
        )
        raise HTTPException(status_code=422, detail=f"Malformed agent manifest: {e}")
    _main._store.add_or_replace_team_agent(
        team_id, agent, on_replaced=_generated_manifest_cleanup(team_id)
    )
    return _main.enrich_roster_agent(agent)


def remove_agent_from_roster(team_id: str, agent_name: str):
    """Remove a single agent from the team roster by name (§5.3).

    The ``:path`` converter lets roster names that contain ``/`` (e.g.
    "Backend — API/OpenAPI Specialist") match instead of 404-ing on the slash. If the
    removed agent was **generated** (installed into the live registry via the LLM save
    path's ``register_team_manifests``), its in-process manifest is also unregistered
    so catalog/invoke consumers stop resolving it — run under the team lock before the
    delete commits, so a concurrent chat-save can't re-add+register the same
    deterministic id in the gap. Registry-source agents are left in the registry, since
    they exist there independently of this team.

    Preconditions: ``team_id`` and ``agent_name`` are non-empty strings.
    Postconditions: ``204`` and the named agent removed from the roster; ``404`` when
        the team is unknown or no roster entry has that name (roster unchanged).
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    team = _main._store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    deleted = _main._store.delete_team_agent(
        team_id, agent_name, on_deleted=_generated_manifest_cleanup(team_id)
    )
    if deleted is None:
        raise HTTPException(status_code=404, detail=f"Agent not on roster: {agent_name}")
    return Response(status_code=204)


def update_roster_agent(team_id: str, agent_name: str, req: UpdateAgentRequest):
    """Inline roster edit endpoint (legacy contract).

    Persona fields now live on ``AgentManifest``; roster rows are thin refs only.
    Any request that supplies persona fields is rejected. An empty body is a no-op
    that returns the current enriched agent.

    Preconditions: ``team_id`` and ``agent_name`` are non-empty strings.
    Postconditions: ``200`` with the enriched agent when the body is empty;
        ``400`` when any persona field is supplied; ``404`` if the team or agent
        is unknown (roster unchanged).
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    team = _main._store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    if req.model_dump(exclude_unset=True):
        raise HTTPException(
            status_code=400,
            detail="Roster persona edits are not supported; AgentManifest is the source of truth",
        )

    current = next((a for a in team.agents if a.agent_name == agent_name), None)
    if current is None:
        raise HTTPException(status_code=404, detail=f"Agent not on roster: {agent_name}")
    return _main.enrich_roster_agent(current)
