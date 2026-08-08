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
        provisioned, and ``200`` is returned with the created team. On a
        non-``HTTPException`` provisioning failure, the team row is removed
        (best-effort — a rollback failure is logged but never masks the
        ``500``) and an ``HTTPException(500)`` is raised. If provisioning
        itself raises an ``HTTPException`` (an intentional status, not an
        outage), it propagates unchanged with **no** rollback — the team row
        is left in place for the caller to act on.
    Invariants: when provisioning fails with a non-``HTTPException`` error and
        the compensating delete succeeds, no team row remains in Postgres
        without corresponding infrastructure. The delete is best-effort, so a
        rollback failure is logged but the row may remain — see
        Postconditions.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    team = _main._store.create_team(name=req.name, description=req.description)
    try:
        _main.provision_team(team.team_id)
    except HTTPException:
        # An intentionally raised HTTP status from provisioning (e.g. a 409
        # conflict) is not an infrastructure outage — propagate it unchanged
        # rather than flattening it into a generic 500.
        raise
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
        ``AgenticTeamAgent`` (empty if no agents have been added yet); ``404``
        if the team is not found.
    Invariants: none.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    team = _main._store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return team.agents


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
        manifests.append(build_agent_manifest(team_id, a))
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
    """Project a registry ``AgentManifest`` into a roster agent (Agent Studio §5.3).

    Preconditions: ``manifest`` is a registered manifest.
    Postconditions: returns an ``AgenticTeamAgent`` with ``source == "registry"``
        and ``manifest_id == manifest.id``. To satisfy ``roster_validation``'s depth
        check (which flags an agent missing ≥3 of skills/capabilities/tools/expertise
        as ``sparse_profile``), the projection fills **two** persona fields that don't
        both depend on the optional ``cognition.tools``: ``skills`` from the manifest
        tags and ``expertise`` from the home ``team`` (always present). So a tagged
        manifest with no cognition tools — the common catalog shape — still passes.
        ``tools`` carries ``cognition.tools`` when present.

    v1 scope boundary: the manifest's typed ``inputs`` / ``outputs`` schema_refs are intentionally
    **not** projected here. In v1 a registry roster entry runs as a free-text LLM persona (built from
    the projected ``role`` / ``skills`` / ``tools`` fields), so its declared typed IO is dropped at
    this boundary rather than marshalled through the DAG. Executing a registry agent through its typed
    contract is deferred — see
    ``system_design/adr/ADR-008-typed-io-registry-agents-in-free-text-dag.md``. Do not add a
    schema-preserving branch here without first resolving that spike.
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
        role=manifest.summary or manifest.name,
        # ``tags``/``cognition.tools`` are non-Optional ``list[str]`` on a validated
        # manifest, but guard ``or []`` / ``and`` defensively at this projection
        # boundary so a degenerate (e.g. ``model_construct``-built) manifest can't
        # pass ``None`` into ``list(...)`` or the model field.
        skills=manifest.tags or [],
        tools=list(manifest.cognition.tools)
        if manifest.cognition and manifest.cognition.tools
        else [],
        # ``team`` is a required, non-empty str on AgentManifest; the guard is
        # belt-and-suspenders so a degenerate empty team can never inject ``[""]``.
        expertise=[manifest.team] if manifest.team else [],
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

        get_registry().unregister(build_agent_manifest(team_id, agent).id)
    except Exception:
        logger.warning(
            "Failed to unregister stale generated manifest for agent %s in team %s",
            agent.agent_name,
            team_id,
            exc_info=True,
        )


def _reregister_generated_manifest(team_id: str, agent: AgenticTeamAgent) -> None:
    """Best-effort: refresh a generated agent's in-process manifest after an in-place edit.

    The inline-edit route (``update_roster_agent``) keeps the same ``agent_name`` — so
    the manifest ``id`` (keyed on ``team_id + agent_name``) is stable — but changes
    ``role``, which ``build_agent_manifest`` projects into the manifest ``summary``.
    Re-registering the rebuilt manifest overwrites the entry in place so the Agent
    Console catalog / ``/api/agents/{id}/invoke`` route reflect the edit, instead of
    serving the stale pre-edit summary until an unrelated chat-save re-registers the
    whole roster. Registry-source agents are never re-registered here (their manifest
    is owned by the catalog, not this team).

    Preconditions: ``agent.source == SOURCE_GENERATED`` (caller checks).
    Postconditions: the agent's manifest is (re)registered if the registry is
        reachable. A registry failure is logged, **never raised** — so this
        best-effort refresh can neither 500 the request nor roll back the committed
        roster edit. (Unlike chat-save ``register_team_manifests``, which raises
        so the roster write rolls back; an in-place role edit still commits even
        if the catalog refresh fails — a later chat-save re-registers the roster.)
    """
    try:
        from agent_registry import get_registry

        get_registry().register(build_agent_manifest(team_id, agent))
    except Exception:
        logger.warning(
            "Failed to re-register edited generated manifest for agent %s in team %s",
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
    """Add a registered agent to the team roster, projected from its manifest (§5.3).

    Re-adding the same manifest updates that roster entry in place. If this replaces a
    *generated* agent of the same name, that generated agent's stale in-process
    manifest is unregistered (the new entry is registry-source and resolves on its
    own) — the cleanup runs in the store's locked transaction against the row actually
    replaced, so it can't race a concurrent chat-save's register.

    Preconditions: ``req.manifest_id`` is non-empty (enforced by the request model).
    Postconditions: ``201`` with the projected roster agent persisted on the roster;
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
    # form; the manifest-id-only "already on roster" guards on the callers miss it
    # because a generated row carries ``manifest_id=None``) AND for another team's
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
    return agent


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
    """Inline-edit a roster agent's projected fields for this team (spec §3, Stage 3).

    Every field on ``req`` is optional; only the ones supplied overwrite the
    existing row (unset fields keep their current value). Works for either
    ``source`` — a ``generated`` agent's fields are its only definition, and a
    ``registry`` agent's fields may be overridden per-team without touching the
    catalog manifest it was projected from; ``source``/``manifest_id`` themselves
    are never changed by this route.

    Preconditions: ``team_id`` and ``agent_name`` are non-empty strings.
    Postconditions: ``200`` with the updated agent persisted in place (all other
        roster rows unchanged), and — for a ``generated`` agent — its in-process
        registry manifest refreshed so the catalog/invoke surfaces reflect the edit;
        ``404`` if the team is unknown or no roster entry has that name (roster
        unchanged); ``422`` if the merged row would violate the ``AgenticTeamAgent``
        contract (e.g. an explicit ``role: null``), so a malformed edit can't persist
        a row that later fails to deserialize (roster unchanged).

    Concurrency: the read-merge-write is delegated to ``update_team_agent``, which
        runs ``_merge`` on the current row **under the team lock** — so a concurrent
        roster write for the same agent (e.g. a chat-save filling ``skills`` while
        this saves a ``role`` edit) can't be clobbered by a merge over a pre-lock
        snapshot, and stale ``source``/``manifest_id`` provenance can't be resurrected.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    if not _main._store.get_team(team_id):
        raise HTTPException(status_code=404, detail="Team not found")

    def _merge(current: AgenticTeamAgent) -> AgenticTeamAgent:
        # Re-validate the merged row rather than ``model_copy(update=...)`` (which
        # skips validation): ``UpdateAgentRequest`` fields are ``Optional`` for
        # partial-update semantics, so an explicit ``{"role": null}`` would otherwise
        # write a row whose required ``role`` is ``None`` and 500 every later
        # ``model_validate`` of the roster. Merging over the lock-read ``current``
        # preserves ``source``/``manifest_id`` (never in the request model), so a
        # per-team field override can't change provenance. Raises ``ValidationError``
        # on a bad patch — caught below and surfaced as ``422`` (the store rolls back).
        updates = req.model_dump(exclude_unset=True)
        return AgenticTeamAgent.model_validate({**current.model_dump(), **updates})

    def _reregister(updated: AgenticTeamAgent) -> None:
        # Keep the live registry in lockstep for a generated agent whose projected
        # summary (its role) may have changed — mirrors the from-registry/DELETE
        # routes' registry reconciliation, run under the same lock as the write.
        if updated.source == SOURCE_GENERATED:
            _reregister_generated_manifest(team_id, updated)

    try:
        updated = _main._store.update_team_agent(
            team_id, agent_name, _merge, on_updated=_reregister
        )
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=f"Invalid agent update: {e.errors()}")
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Agent not on roster: {agent_name}")
    return updated
