"""FastAPI application for the Agentic Team Provisioning service."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional
from urllib.parse import quote

from fastapi import HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import ValidationError

from agent_registry.models import AgentManifest
from agent_team_studio.agentic_team_provisioning.agent_env_provisioning import (
    schedule_provision_step_agents,
)
from agent_team_studio.agentic_team_provisioning.assistant.agent import ProcessDesignerAgent
from agent_team_studio.agentic_team_provisioning.assistant.store import AgenticTeamStore
from agent_team_studio.agentic_team_provisioning.infrastructure import (
    TeamInfrastructure,
    get_team_infrastructure,
    provision_team,
)
from agent_team_studio.agentic_team_provisioning.manifest_generation import (
    build_agent_manifest,
    is_generated_manifest,
    register_team_manifests,
)
from agent_team_studio.agentic_team_provisioning.models import (
    SOURCE_GENERATED,
    SOURCE_REGISTRY,
    AddAgentFromRegistryRequest,
    AgentEnvProvisionSummary,
    AgenticTeam,
    AgenticTeamAgent,
    AgentQualityScore,
    AssetInfo,
    ConversationStateResponse,
    ConversationSummaryResponse,
    CreateConversationRequest,
    CreateFormRecordRequest,
    CreateTeamRequest,
    CreateTeamResponse,
    CreateTestChatSessionRequest,
    FormRecord,
    GeneratedManifestsResponse,
    ProcessDefinition,
    ProcessOutput,
    ProcessStatus,
    ProcessTrigger,
    RateMessageRequest,
    RecommendAgentsResponse,
    RecommendedAgent,
    RenameTestChatSessionRequest,
    RosterValidationResult,
    SendMessageRequest,
    SendTestChatMessageRequest,
    SetConversationProcessRequest,
    SetTeamModeRequest,
    StartPipelineRunRequest,
    SubmitPipelineInputRequest,
    SubmitTeamAnswersRequest,
    TeamDetailResponse,
    TeamJobDetail,
    TeamJobSummary,
    TeamPendingQuestion,
    TeamSummary,
    TestChatMessage,
    TestChatSession,
    TestChatSessionDetail,
    TestPipelineRun,
    UpdateAgentRequest,
    UpdateFormRecordRequest,
)
from agent_team_studio.agentic_team_provisioning.postgres import SCHEMA as AGENTIC_POSTGRES_SCHEMA
from agent_team_studio.agentic_team_provisioning.runtime.agent_builder import (
    build_agent as _build_test_agent,
)
from agent_team_studio.agentic_team_provisioning.runtime.agent_builder import (
    call_agent as _call_test_agent,
)
from agent_team_studio.agentic_team_provisioning.runtime.agent_builder import (
    generate_starter_prompts,
)
from agent_team_studio.agentic_team_provisioning.runtime.pipeline_runner import get_pipeline_runner
from agent_team_studio.agentic_team_provisioning.testing.store import get_test_store
from shared.app import create_team_app
from shared.env_config import env_int

logger = logging.getLogger(__name__)


def _startup() -> None:
    """Start the Temporal worker backstop (best-effort).

    The team_service entrypoint normally starts the worker via
    ``TEAM_TEMPORAL_WORKER_MODULE`` before uvicorn accepts requests; this backstop
    covers running the app standalone (``uvicorn ...:app``).

    Preconditions:
        - None (safe to call once at app startup).

    Postconditions:
        - Starts the worker thread when Temporal is enabled; a no-op when
          ``TEMPORAL_ADDRESS`` is unset. Never raises — any failure is logged as a
          warning so it cannot abort app boot (this runs as an ``on_startup`` hook).
    """
    try:
        from agent_team_studio.agentic_team_provisioning.temporal.worker import (
            start_agentic_team_provisioning_temporal_worker_thread,
        )

        start_agentic_team_provisioning_temporal_worker_thread()
    except Exception:
        logger.warning(
            "agentic_team_provisioning Temporal worker start (lifespan backstop) failed",
            exc_info=True,
        )


app = create_team_app(
    service_name="agentic-team-provisioning",
    team_key="agentic_team_provisioning",
    title="Agentic Team Provisioning API",
    description="Create agentic teams and define their processes through conversation",
    version="0.1.0",
    postgres_schema=AGENTIC_POSTGRES_SCHEMA,
    on_startup=_startup,
)

_store = AgenticTeamStore()
_agent = ProcessDesignerAgent()

# Interactive testing mode singletons
_test_store = get_test_store()
_pipeline_runner = get_pipeline_runner(_test_store)

# Retroactive provisioning: ensure all existing teams have infrastructure and
# that their generated agents are registered in the live registry (rosters are
# Postgres-backed, so this re-registers them after a process restart). Each team
# is isolated, and within a team infrastructure recovery and registry restoration
# are decoupled — a transient infrastructure failure must not hide an otherwise
# usable roster from the registry for the lifetime of the process.
try:
    _existing_teams = _store.list_teams()
except Exception as _e:
    logger.warning("Could not list existing teams for retroactive provisioning: %s", _e)
    _existing_teams = []

for _team_row in _existing_teams:
    _tid = _team_row["team_id"]
    try:
        get_team_infrastructure(_tid)
    except Exception as _e:
        logger.warning("Could not retroactively provision infrastructure for team %s: %s", _tid, _e)
    try:
        _team = _store.get_team(_tid)
        if _team is not None and _team.agents:
            register_team_manifests(_tid, _team.agents)
    except Exception as _e:
        logger.warning("Could not register generated manifests for team %s: %s", _tid, _e)

# Restart cleanup: a pipeline test run whose worker thread died (restart or crash)
# leaves its DB row stuck in an active state with no live waiter. Reap orphans whose
# heartbeat has gone stale so they fail cleanly instead of stranding forever. Safe with
# multiple workers (advisory-locked, heartbeat-based) — a live sibling worker's run is
# never touched. Best-effort so a reaper hiccup can't break module import.
try:
    _reaped = _pipeline_runner.reap_orphaned_runs()
    if _reaped:
        logger.warning("Reaped %d orphaned pipeline run(s) on startup", _reaped)
except Exception as _e:
    logger.warning("Could not reap orphaned pipeline runs on startup: %s", _e)

GREETING = (
    "Hello! I'm your Process Designer assistant. I'll help you design an agentic "
    "team — its agents and processes. Tell me what the team should do at a high "
    "level, and we'll work through the agents you need and the processes they'll run."
)

DEFAULT_SUGGESTIONS = [
    "I want to define a customer onboarding process",
    "Help me create a content review workflow",
    "I need a process for handling support tickets",
]


def _save_agents_from_llm(team_id: str, agents_data: list[dict[str, Any]] | None) -> None:
    """Persist the LLM ``agents`` block, preserving any registry-source roster entries.

    The chat round-trips only generated agents, so a naive full replace would drop
    the registry agents a user added via the from-registry endpoint (Agent Studio
    §5.3). We therefore merge: existing ``source == "registry"`` entries are kept, and
    the LLM's generated agents are layered on top — a generated agent that collides
    by name with a preserved registry agent is dropped, so the explicitly-added
    registry agent wins.

    Concurrency: the read-merge-write is delegated to ``merge_generated_agents``,
    which runs it in a single transaction under a ``SELECT ... FOR UPDATE`` lock on
    the team row, so concurrent saves for the *same* team serialize rather than
    racing — neither can rewrite from a stale snapshot and drop the other's writes.
    The registry registration runs in the same locked transaction (via the
    ``on_merged`` hook), so all registry mutations for a team are serialized with the
    single-agent routes' registry cleanup — a chat-save register can't interleave
    with a concurrent add/delete cleanup.
    """
    if not agents_data:
        return
    generated: list[AgenticTeamAgent] = []
    for a in agents_data:
        name = a.get("agent_name", "")
        if not name:
            continue
        generated.append(
            AgenticTeamAgent(
                agent_name=name,
                role=a.get("role", ""),
                skills=a.get("skills", []),
                capabilities=a.get("capabilities", []),
                tools=a.get("tools", []),
                expertise=a.get("expertise", []),
            )
        )
    if not generated:
        return

    def _register(merged: list[AgenticTeamAgent], conn) -> None:
        # Install the generated agents into the live registry so the Agent Console
        # catalog and /api/agents/{id}/invoke resolve them (skips registry-source
        # entries internally). Runs under the team lock on the roster connection so
        # it's serialized with the single-agent routes' registry cleanup and the
        # dynamic-store replace joins this transaction — a commit failure rolls
        # roster + registry back together. Raises on registry failure so
        # merge_generated_agents rolls back the roster write and keeps both
        # stores consistent.
        register_team_manifests(team_id, merged, conn=conn)

    # Merge under a team-row lock so the read (preserve registry agents), the write,
    # and the registry register all happen in one atomic, serialized transaction.
    _store.merge_generated_agents(team_id, generated, on_merged=_register)


def _after_process_saved(team_id: str, process: ProcessDefinition) -> None:
    """Provision per-step agent environments via agent_provisioning_team (background)."""
    schedule_provision_step_agents(team_id, process, _store)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    return {"status": "ok", "service": "agentic-team-provisioning"}


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------


@app.post("/teams", response_model=CreateTeamResponse)
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
    team = _store.create_team(name=req.name, description=req.description)
    try:
        provision_team(team.team_id)
    except Exception as exc:
        logger.exception(
            "Failed to provision infrastructure for team %s; rolling back team row",
            team.team_id,
        )
        try:
            _store.delete_team(team.team_id)
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


@app.get("/teams", response_model=list[TeamSummary])
def list_teams():
    """List all agentic teams.

    Preconditions: none.
    Postconditions: ``200`` with a ``TeamSummary`` for every persisted team, in the
        store's default order; an empty list if no teams exist.
    """
    rows = _store.list_teams()
    return [TeamSummary(**r) for r in rows]


@app.get("/teams/{team_id}", response_model=TeamDetailResponse)
def get_team(team_id: str):
    """Retrieve a single agentic team by id.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: ``200`` with the full ``TeamDetailResponse`` when the team
        exists; ``404`` if no team with the given id is found.
    """
    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return TeamDetailResponse(team=team)


# ---------------------------------------------------------------------------
# Team agents pool
# ---------------------------------------------------------------------------


@app.get("/teams/{team_id}/agents", response_model=list[AgenticTeamAgent])
def list_team_agents(team_id: str):
    """Named agents pool (roster) for this team.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: ``200`` with the team's roster as a list of
        ``AgenticTeamAgent`` (empty if no agents have been added yet); ``404``
        if the team is not found.
    Invariants: none.
    """
    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return team.agents


@app.get("/teams/{team_id}/agents/manifests", response_model=GeneratedManifestsResponse)
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

    team = _store.get_team(team_id)
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


@app.get("/teams/{team_id}/roster/validation", response_model=RosterValidationResult)
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
    from agent_team_studio.agentic_team_provisioning.roster_validation import validate_roster

    team = _store.get_team(team_id)
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


@app.post("/teams/{team_id}/agents/from-registry", response_model=AgenticTeamAgent, status_code=201)
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

    team = _store.get_team(team_id)
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
        agent = _roster_agent_from_manifest(manifest)
    except (ValueError, ValidationError) as e:
        logger.warning(
            "Malformed registered manifest %s for team %s: %s", req.manifest_id, team_id, e
        )
        raise HTTPException(status_code=422, detail=f"Malformed agent manifest: {e}")
    _store.add_or_replace_team_agent(
        team_id, agent, on_replaced=_generated_manifest_cleanup(team_id)
    )
    return agent


@app.delete("/teams/{team_id}/agents/{agent_name:path}", status_code=204)
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
    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    deleted = _store.delete_team_agent(
        team_id, agent_name, on_deleted=_generated_manifest_cleanup(team_id)
    )
    if deleted is None:
        raise HTTPException(status_code=404, detail=f"Agent not on roster: {agent_name}")
    return Response(status_code=204)


@app.put("/teams/{team_id}/agents/{agent_name:path}", response_model=AgenticTeamAgent)
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
    if not _store.get_team(team_id):
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
        updated = _store.update_team_agent(team_id, agent_name, _merge, on_updated=_reregister)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=f"Invalid agent update: {e.errors()}")
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Agent not on roster: {agent_name}")
    return updated


# ---------------------------------------------------------------------------
# Processes (direct CRUD — processes can also be created via conversation)
# ---------------------------------------------------------------------------


@app.get("/teams/{team_id}/processes", response_model=list[ProcessDefinition])
def list_processes(team_id: str):
    """List all processes defined for a team.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: ``200`` with the team's processes as a list of
        ``ProcessDefinition`` (empty if none have been created yet); ``404``
        if the team is not found.
    """
    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return team.processes


@app.get("/processes/{process_id}", response_model=ProcessDefinition)
def get_process(process_id: str):
    """Retrieve a single process definition by id.

    Note: unlike the team-scoped routes, this one is looked up globally by
    ``process_id`` alone (no ``team_id`` in the path) — the visual editor and
    conversation flows address a process directly once they know its id.

    Preconditions: ``process_id`` is a non-empty string.
    Postconditions: ``200`` with the ``ProcessDefinition``; ``404`` if no
        process with that id exists.
    """
    process = _store.get_process(process_id)
    if not process:
        raise HTTPException(status_code=404, detail="Process not found")
    return process


@app.post("/teams/{team_id}/processes", response_model=ProcessDefinition, status_code=201)
def create_process(team_id: str):
    """Create a new blank process for the team.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: ``201`` with a fresh ``ProcessDefinition`` (a new UUID
        ``process_id``, name "New Process", no steps, ``status=DRAFT``)
        persisted under the team; ``404`` if the team is not found (no process
        created). Side effect: inserts a new process row via
        ``_store.save_process``.
    """
    team = _store.get_team(team_id)
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
    _store.save_process(team_id, process)
    return process


@app.put("/processes/{process_id}", response_model=ProcessDefinition)
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
    existing = _store.get_process(process_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Process not found")
    if process.process_id != process_id:
        raise HTTPException(status_code=400, detail="process_id in body must match URL")
    # Find team_id from the store
    team_id = _store.get_process_team_id(process_id)
    if not team_id:
        raise HTTPException(status_code=404, detail="Process team not found")
    _store.save_process(team_id, process)
    _after_process_saved(team_id, process)
    return process


@app.post(
    "/processes/{process_id}/steps/{step_id}/recommend-agents",
    response_model=RecommendAgentsResponse,
)
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
    process = _store.get_process(process_id)
    if not process:
        raise HTTPException(status_code=404, detail="Process not found")
    step = next((s for s in process.steps if s.step_id == step_id), None)
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")

    team_id = _store.get_process_team_id(process_id)
    recommendations: list[RecommendedAgent] = []

    # Recommend matching roster agents
    if team_id:
        team = _store.get_team(team_id)
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

    # Sort by score descending
    recommendations.sort(key=lambda r: -r.match_score)

    return RecommendAgentsResponse(
        step_id=step_id,
        step_name=step.name,
        recommended_agents=recommendations[:10],
    )


# ---------------------------------------------------------------------------
# Conversations (chat-based process design)
# ---------------------------------------------------------------------------


def _build_state_response(
    conversation_id: str,
    team_id: str,
    process: Optional[ProcessDefinition],
    suggested_questions: list[str],
) -> ConversationStateResponse:
    messages = _store.get_messages(conversation_id)
    return ConversationStateResponse(
        conversation_id=conversation_id,
        team_id=team_id,
        messages=messages,
        current_process=process,
        suggested_questions=suggested_questions,
    )


@app.post("/conversations", response_model=ConversationStateResponse)
def create_conversation(req: CreateConversationRequest):
    """Start a new conversation for a team, optionally seeded with an initial message.

    Preconditions: ``req.team_id`` refers to an existing team.
    Postconditions: ``200`` with the conversation's state; ``404`` if the team is
        unknown (nothing persisted). When ``req.initial_message`` is given, the
        user/assistant chat turn is appended to the store *before* the LLM's
        roster is saved via ``_save_agents_from_llm``; if that save fails because
        the agent registry is unavailable, this raises ``503`` instead of an
        opaque ``500`` — but the conversation is left with the chat turn already
        persisted and the roster/registry write rolled back (partial state). A
        client retrying the same message re-processes it against a conversation
        that already contains the prior turn.
    """
    # Validate team exists
    team = _store.get_team(req.team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    conversation_id = _store.create_conversation(team_id=req.team_id)

    if req.initial_message:
        _store.append_message(conversation_id, "user", req.initial_message)

        existing_agents = [
            {"agent_name": a.agent_name, "role": a.role}
            for a in _store.list_team_agents(req.team_id)
        ] or None

        reply, process, suggestions, agents_data = _agent.respond(
            conversation_history=[],
            current_process=None,
            user_message=req.initial_message,
            current_agents=existing_agents,
        )

        _store.append_message(conversation_id, "assistant", reply)
        try:
            _save_agents_from_llm(req.team_id, agents_data)
        except Exception as e:
            logger.warning(
                "Roster save failed for team %s after conversation %s turn: %s",
                req.team_id,
                conversation_id,
                e,
            )
            raise HTTPException(status_code=503, detail="Agent registry unavailable") from e
        if process:
            _store.save_process(req.team_id, process)
            _store.set_conversation_process(conversation_id, process.process_id)
            _after_process_saved(req.team_id, process)

        return _build_state_response(conversation_id, req.team_id, process, suggestions)

    # No initial message — just add the greeting
    _store.append_message(conversation_id, "assistant", GREETING)
    return _build_state_response(conversation_id, req.team_id, None, DEFAULT_SUGGESTIONS)


@app.post("/conversations/{conversation_id}/messages", response_model=ConversationStateResponse)
def send_message(conversation_id: str, req: SendMessageRequest):
    """Send a user message on an existing conversation and get the assistant's reply.

    Preconditions: ``conversation_id`` refers to an existing conversation.
    Postconditions: ``200`` with the conversation's updated state; ``404`` if the
        conversation is unknown (nothing persisted). The user/assistant chat turn
        is appended to the store *before* the LLM's roster is saved via
        ``_save_agents_from_llm``; if that save fails because the agent registry
        is unavailable, this raises ``503`` instead of an opaque ``500`` — but the
        conversation is left with the chat turn already persisted and the
        roster/registry write rolled back (partial state). A client retrying the
        same message re-processes it against a conversation that already contains
        the prior turn.
    """
    team_id = _store.get_conversation_team_id(conversation_id)
    if not team_id:
        raise HTTPException(status_code=404, detail="Conversation not found")

    process_id = _store.get_conversation_process_id(conversation_id)
    current_process = _store.get_process(process_id) if process_id else None

    existing_agents = [
        {"agent_name": a.agent_name, "role": a.role} for a in _store.list_team_agents(team_id)
    ] or None

    existing_messages = _store.get_messages(conversation_id)
    history = [(m.role, m.content) for m in existing_messages]

    _store.append_message(conversation_id, "user", req.message)

    reply, updated_process, suggestions, agents_data = _agent.respond(
        conversation_history=history,
        current_process=current_process,
        user_message=req.message,
        current_agents=existing_agents,
    )

    _store.append_message(conversation_id, "assistant", reply)
    try:
        _save_agents_from_llm(team_id, agents_data)
    except Exception as e:
        logger.warning(
            "Roster save failed for team %s after conversation %s turn: %s",
            team_id,
            conversation_id,
            e,
        )
        raise HTTPException(status_code=503, detail="Agent registry unavailable") from e

    effective_process = current_process
    if updated_process:
        _store.save_process(team_id, updated_process)
        _store.set_conversation_process(conversation_id, updated_process.process_id)
        effective_process = updated_process
        _after_process_saved(team_id, updated_process)

    return _build_state_response(conversation_id, team_id, effective_process, suggestions)


@app.put("/conversations/{conversation_id}/process")
def set_conversation_process(conversation_id: str, req: SetConversationProcessRequest):
    """Link a process to the active conversation so chat stays in sync with the visual editor.

    Preconditions: ``req.process_id`` is non-empty (enforced by the request model;
        a missing/blank value is a ``422`` before this handler runs).
    Postconditions: ``200`` with the linked ``conversation_id``/``process_id`` pair;
        ``404`` if the conversation or the process is unknown (link unchanged); ``403``
        if the process belongs to a different team than the conversation (link
        unchanged) — a conversation may only be linked to its own team's processes.
    """
    team_id = _store.get_conversation_team_id(conversation_id)
    if not team_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    process = _store.get_process(req.process_id)
    if not process:
        raise HTTPException(status_code=404, detail="Process not found")
    if _store.get_process_team_id(req.process_id) != team_id:
        raise HTTPException(
            status_code=403, detail="Process does not belong to this conversation's team"
        )
    _store.set_conversation_process(conversation_id, req.process_id)
    return {"conversation_id": conversation_id, "process_id": req.process_id}


@app.get("/conversations/{conversation_id}", response_model=ConversationStateResponse)
def get_conversation(conversation_id: str):
    team_id = _store.get_conversation_team_id(conversation_id)
    if not team_id:
        raise HTTPException(status_code=404, detail="Conversation not found")

    process_id = _store.get_conversation_process_id(conversation_id)
    process = _store.get_process(process_id) if process_id else None

    return _build_state_response(conversation_id, team_id, process, [])


@app.get("/teams/{team_id}/conversations", response_model=list[ConversationSummaryResponse])
def list_conversations(team_id: str):
    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    rows = _store.list_conversations(team_id)
    return [ConversationSummaryResponse(**r) for r in rows]


@app.get("/teams/{team_id}/agent-environments", response_model=List[AgentEnvProvisionSummary])
def list_team_agent_environments(team_id: str):
    """Per-step agent provisioning status (Agent Provisioning team / sandboxed envs)."""
    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    rows = _store.list_agent_env_provisions(team_id)
    return [AgentEnvProvisionSummary(**r) for r in rows]


# ---------------------------------------------------------------------------
# Per-team infrastructure helper
# ---------------------------------------------------------------------------


def _get_infra_or_404(team_id: str) -> TeamInfrastructure:
    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return get_team_infrastructure(team_id)


def _get_team_or_404(team_id: str) -> AgenticTeam:
    """Look up a team, raising 404 if it doesn't exist.

    Preconditions: none.
    Postconditions: returns the team when found; otherwise raises HTTPException(404)
        and never returns.
    """
    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return team


# ---------------------------------------------------------------------------
# Team / Job Status
# ---------------------------------------------------------------------------


@app.get("/teams/{team_id}/jobs", response_model=List[TeamJobSummary])
def list_team_jobs(team_id: str):
    """List all jobs for a provisioned team."""
    infra = _get_infra_or_404(team_id)
    raw_jobs = infra.job_client.list_jobs() or []
    return [
        TeamJobSummary(
            job_id=j.get("job_id", ""),
            status=j.get("status", "unknown"),
            created_at=j.get("created_at", ""),
            updated_at=j.get("updated_at", ""),
        )
        for j in raw_jobs
    ]


@app.get("/teams/{team_id}/jobs/{job_id}", response_model=TeamJobDetail)
def get_team_job(team_id: str, job_id: str):
    """Get a single job's detail."""
    infra = _get_infra_or_404(team_id)
    job = infra.job_client.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return TeamJobDetail(
        job_id=job.get("job_id", job_id),
        status=job.get("status", "unknown"),
        data=job,
    )


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------


@app.get("/teams/{team_id}/questions", response_model=List[TeamPendingQuestion])
def list_team_questions(team_id: str):
    """Collect pending questions from all active jobs for a team."""
    infra = _get_infra_or_404(team_id)
    active_jobs = infra.job_client.list_jobs(statuses=["pending", "running"]) or []
    result: List[TeamPendingQuestion] = []
    for j in active_jobs:
        jid = j.get("job_id", "")
        for q in j.get("pending_questions", []):
            result.append(TeamPendingQuestion(job_id=jid, question=q))
    return result


@app.post("/teams/{team_id}/questions/{job_id}/answers")
def submit_team_answers(team_id: str, job_id: str, req: SubmitTeamAnswersRequest):
    """Submit answers to pending questions for a job."""
    infra = _get_infra_or_404(team_id)
    job = infra.job_client.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    infra.job_client.atomic_update(
        job_id,
        merge_fields={"pending_questions": [], "waiting_for_answers": False},
        append_to={"submitted_answers": req.answers},
    )
    return {"job_id": job_id, "message": "Answers submitted"}


# ---------------------------------------------------------------------------
# Assets (File System)
# ---------------------------------------------------------------------------

_DEFAULT_MAX_ASSET_BYTES = 10 * 1024 * 1024  # 10 MiB
_ASSET_UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MiB, read granularity while enforcing the limit


def _max_asset_upload_bytes() -> int:
    """Configured per-asset upload ceiling (``AGENTIC_TEAM_MAX_ASSET_BYTES``).

    Postconditions: returns a positive int — the parsed env var when set and
    valid, else ``_DEFAULT_MAX_ASSET_BYTES`` (per ``shared.env_config.env_int``:
    garbage or unset falls back to the default, never raises).
    """
    return env_int("AGENTIC_TEAM_MAX_ASSET_BYTES", _DEFAULT_MAX_ASSET_BYTES, floor=1)


def _safe_asset_name(name: str) -> str:
    """Sanitize asset name to prevent path traversal."""
    sanitized = Path(name).name
    if not sanitized or sanitized in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid asset name")
    return sanitized


@app.get("/teams/{team_id}/assets", response_model=List[AssetInfo])
def list_team_assets(team_id: str):
    """List files in the team's asset directory."""
    infra = _get_infra_or_404(team_id)
    assets: List[AssetInfo] = []
    if infra.assets_dir.is_dir():
        for p in sorted(infra.assets_dir.iterdir()):
            if p.is_file():
                stat = p.stat()
                assets.append(
                    AssetInfo(
                        name=p.name,
                        size_bytes=stat.st_size,
                        modified_at=datetime.fromtimestamp(
                            stat.st_mtime, tz=timezone.utc
                        ).isoformat(),
                    )
                )
    return assets


@app.get("/teams/{team_id}/assets/{name}")
def download_team_asset(team_id: str, name: str):
    """Download a specific asset file.

    Preconditions: none beyond ``team_id``/``name`` being valid path segments.
    Postconditions: ``200`` streaming the file's bytes with an RFC 5987-encoded
        ``Content-Disposition`` header (safe for names containing quotes or
        non-ASCII characters, which would otherwise malform a raw ``filename=``
        header); ``404`` if ``name`` sanitizes to an invalid asset name, the
        resolved path escapes ``assets_dir`` (e.g. a symlink inside the
        directory pointing elsewhere on the host), or no such file exists.
    """
    infra = _get_infra_or_404(team_id)
    safe_name = _safe_asset_name(name)
    assets_root = infra.assets_dir.resolve()
    path = (infra.assets_dir / safe_name).resolve()
    if not path.is_relative_to(assets_root) or not path.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")
    encoded_name = quote(safe_name)
    return FileResponse(
        str(path),
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"},
    )


@app.post("/teams/{team_id}/assets", response_model=AssetInfo)
async def upload_team_asset(team_id: str, file: UploadFile):
    """Upload a file to the team's asset directory.

    Preconditions: none beyond a valid multipart upload.
    Postconditions: ``200`` with the stored asset's metadata; ``400`` if the
        filename sanitizes to nothing usable; ``409`` if an asset with the same
        sanitized name already exists (uploads never silently overwrite one
        another); ``413`` if the upload exceeds
        ``AGENTIC_TEAM_MAX_ASSET_BYTES`` (default 10 MiB) — enforced by reading
        in bounded chunks, so an oversized upload is rejected without ever
        buffering the full payload into memory. The filesystem write and stat
        run off the event loop (``asyncio.to_thread``) so a large upload can't
        stall concurrent requests.
    """
    infra = _get_infra_or_404(team_id)
    safe_name = _safe_asset_name(file.filename or "upload")
    dest = infra.assets_dir / safe_name
    if dest.exists():
        raise HTTPException(status_code=409, detail=f"Asset already exists: {safe_name}")

    max_bytes = _max_asset_upload_bytes()
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_ASSET_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="Asset exceeds maximum upload size")
        chunks.append(chunk)
    content = b"".join(chunks)

    await asyncio.to_thread(dest.write_bytes, content)
    stat = await asyncio.to_thread(dest.stat)
    return AssetInfo(
        name=safe_name,
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Form Information (Database)
# ---------------------------------------------------------------------------


@app.get("/teams/{team_id}/forms", response_model=List[str])
def list_team_form_keys(team_id: str):
    """List distinct form keys that have records."""
    infra = _get_infra_or_404(team_id)
    return infra.form_store.list_form_keys()


@app.get("/teams/{team_id}/forms/{form_key}", response_model=List[FormRecord])
def list_team_form_records(team_id: str, form_key: str):
    """Get all records for a form key."""
    infra = _get_infra_or_404(team_id)
    rows = infra.form_store.get_records(form_key)
    return [FormRecord(**r) for r in rows]


@app.post("/teams/{team_id}/forms/{form_key}", response_model=FormRecord, status_code=201)
def create_team_form_record(team_id: str, form_key: str, req: CreateFormRecordRequest):
    """Create a new form record."""
    infra = _get_infra_or_404(team_id)
    record = infra.form_store.create_record(form_key, req.data)
    return FormRecord(**record)


@app.put("/teams/{team_id}/forms/{form_key}/{record_id}", response_model=FormRecord)
def update_team_form_record(
    team_id: str, form_key: str, record_id: str, req: UpdateFormRecordRequest
):
    """Update an existing form record."""
    infra = _get_infra_or_404(team_id)
    if not infra.form_store.update_record(form_key, record_id, req.data):
        raise HTTPException(status_code=404, detail="Record not found")
    record = infra.form_store.get_record(record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found after update")
    return FormRecord(**record)


@app.delete("/teams/{team_id}/forms/{form_key}/{record_id}", status_code=204)
def delete_team_form_record(team_id: str, form_key: str, record_id: str):
    """Delete a form record."""
    infra = _get_infra_or_404(team_id)
    if not infra.form_store.delete_record(form_key, record_id):
        raise HTTPException(status_code=404, detail="Record not found")


# ---------------------------------------------------------------------------
# Interactive Testing Mode
# ---------------------------------------------------------------------------


@app.put("/teams/{team_id}/mode")
def set_team_mode(team_id: str, req: SetTeamModeRequest):
    """Toggle team between development and testing mode."""
    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    _test_store.set_team_mode(team_id, req.mode.value)
    return {"team_id": team_id, "mode": req.mode.value}


# ---------------------------------------------------------------------------
# Agent Chat Testing
# ---------------------------------------------------------------------------


def _find_agent_in_roster(team_id: str, agent_name: str) -> AgenticTeamAgent:
    """Look up an agent by name in the team roster."""
    agents = _store.list_team_agents(team_id)
    for a in agents:
        if a.agent_name == agent_name:
            return a
    raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' not found in team roster")


@app.post("/teams/{team_id}/test-chat/sessions", response_model=TestChatSession, status_code=201)
def create_test_chat_session(team_id: str, req: CreateTestChatSessionRequest):
    """Create a new chat test session for an agent."""
    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    _find_agent_in_roster(team_id, req.agent_name)
    session_id = str(uuid.uuid4())
    row = _test_store.create_chat_session(session_id, team_id, req.agent_name)
    return TestChatSession(**row)


@app.get("/teams/{team_id}/test-chat/sessions", response_model=List[TestChatSession])
def list_test_chat_sessions(team_id: str, agent_name: Optional[str] = None):
    """List chat test sessions for a team, optionally filtered by agent.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: ``200`` with a ``TestChatSession`` for each session belonging
        to ``team_id`` (filtered to ``agent_name`` when given); ``404`` if
        ``team_id`` is unknown, consistent with the sibling test-chat endpoints.
    """
    _get_team_or_404(team_id)
    rows = _test_store.list_chat_sessions(team_id, agent_name=agent_name)
    return [TestChatSession(**r) for r in rows]


@app.get("/teams/{team_id}/test-chat/sessions/{session_id}", response_model=TestChatSessionDetail)
def get_test_chat_session(team_id: str, session_id: str):
    """Get a chat session with full message history and suggested prompts.

    Preconditions: ``team_id`` and ``session_id`` are non-empty strings.
    Postconditions: ``200`` with the session, its messages, and starter prompts
        (only generated when the session has no messages yet); ``404`` if the
        session doesn't exist or belongs to a different team. If starter-prompt
        generation raises a 404 because the session's agent isn't on the
        roster, the prompts list is empty rather than failing the request; any
        other failure (e.g. a registry outage) propagates instead of being
        swallowed.
    """
    session_row = _test_store.get_chat_session(session_id)
    if not session_row or session_row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = _test_store.list_chat_messages(session_id)
    session = TestChatSession(**session_row)

    # Generate suggested prompts if no messages yet
    prompts: list[str] = []
    if not messages:
        try:
            agent_def = _find_agent_in_roster(team_id, session.agent_name)
            prompts = generate_starter_prompts(
                agent_def.agent_name, agent_def.role, agent_def.skills, agent_def.expertise
            )
        except HTTPException as exc:
            # Only the genuine "agent not on roster" case falls back to an empty
            # prompt list. Anything else (e.g. a registry 500) is a real failure
            # worth surfacing to the caller, not silently swallowing.
            if exc.status_code != 404:
                raise
            logger.warning(
                "Could not generate starter prompts for session %s (agent=%s): %s",
                session_id,
                session.agent_name,
                exc.detail,
            )

    return TestChatSessionDetail(
        session=session,
        messages=[TestChatMessage(**m) for m in messages],
        suggested_prompts=prompts,
    )


@app.put("/teams/{team_id}/test-chat/sessions/{session_id}/name")
def rename_test_chat_session(team_id: str, session_id: str, req: RenameTestChatSessionRequest):
    """Rename a chat test session."""
    session_row = _test_store.get_chat_session(session_id)
    if not session_row or session_row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Session not found")
    _test_store.rename_chat_session(session_id, req.session_name)
    return {"session_id": session_id, "session_name": req.session_name}


@app.delete("/teams/{team_id}/test-chat/sessions/{session_id}", status_code=204)
def delete_test_chat_session(team_id: str, session_id: str):
    """Delete a chat test session and its messages."""
    session_row = _test_store.get_chat_session(session_id)
    if not session_row or session_row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Session not found")
    _test_store.delete_chat_session(session_id)


@app.post("/teams/{team_id}/test-chat/sessions/{session_id}/messages")
def send_test_chat_message(team_id: str, session_id: str, req: SendTestChatMessageRequest):
    """Send a message to an agent and get a synchronous response.

    The full conversation history is sent to the agent for multi-turn context.

    Preconditions: ``team_id``/``session_id`` refer to an existing session;
        ``req.content`` is non-empty (enforced by the request model).
    Postconditions: ``200`` with the session and its full message list,
        including the new user/assistant turn; ``404`` if the session is
        unknown or belongs to a different team; ``502`` if the agent
        invocation fails. The user and assistant messages are persisted
        together as a single turn only after the agent call succeeds, so a
        failed invocation leaves no orphaned user message for a retry to
        duplicate.
    """
    session_row = _test_store.get_chat_session(session_id)
    if not session_row or session_row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Session not found")

    agent_name = session_row["agent_name"]
    agent_def = _find_agent_in_roster(team_id, agent_name)

    # Build conversation context from history plus the pending message. The
    # user message isn't persisted yet, so it's appended directly here rather
    # than read back from the store.
    history = _test_store.list_chat_messages(session_id)
    context_parts = []
    for msg in history:
        prefix = "User" if msg["role"] == "user" else "Assistant"
        context_parts.append(f"{prefix}: {msg['content']}")
    context_parts.append(f"User: {req.content}")
    full_context = "\n\n".join(context_parts)

    # Build and invoke the agent. This local test-chat path has no cognition
    # injector (no proxy / open side channel) and no idempotency ledger, so it
    # uses the plain runtime rather than the cognition-aware wrapper — advisory
    # rules + memory digest are rendered on the gated sandbox invoke path, where
    # the shim opens the channel.
    try:
        agent_instance = _build_test_agent(
            agent_def.agent_name,
            agent_def.role,
            agent_def.skills,
            agent_def.capabilities,
            agent_def.tools,
            agent_def.expertise,
        )
        response_text = _call_test_agent(agent_instance, full_context)
    except Exception as exc:
        logger.exception("Agent invocation failed for test-chat session %s", session_id)
        raise HTTPException(status_code=502, detail="Agent invocation failed") from exc

    # Persist the user and assistant messages together as a complete turn.
    user_msg_id = str(uuid.uuid4())
    _test_store.create_chat_message(user_msg_id, session_id, "user", req.content)
    asst_msg_id = str(uuid.uuid4())
    _test_store.create_chat_message(asst_msg_id, session_id, "assistant", response_text)

    # Return all messages
    all_messages = _test_store.list_chat_messages(session_id)
    return {
        "session": TestChatSession(**session_row),
        "messages": [TestChatMessage(**m) for m in all_messages],
    }


@app.get("/teams/{team_id}/test-chat/sessions/{session_id}/export")
def export_test_chat_session(team_id: str, session_id: str):
    """Export a chat session transcript as Markdown text."""
    session_row = _test_store.get_chat_session(session_id)
    if not session_row or session_row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = _test_store.list_chat_messages(session_id)
    agent_name = session_row["agent_name"]
    session_name = session_row.get("session_name") or f"Chat with {agent_name}"

    lines = [f"# {session_name}", f"Agent: {agent_name}", ""]
    for msg in messages:
        role_label = "**User**" if msg["role"] == "user" else f"**{agent_name}**"
        rating_str = ""
        if msg.get("rating"):
            rating_str = " \u2705" if msg["rating"] == "thumbs_up" else " \u274c"
        lines.append(f"{role_label}{rating_str}:")
        lines.append(msg["content"])
        lines.append("")

    return Response(
        content="\n".join(lines),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{session_id}.md"'},
    )


@app.put("/teams/{team_id}/test-chat/messages/{message_id}/rating")
def rate_test_chat_message(team_id: str, message_id: str, req: RateMessageRequest):
    """Rate an assistant message (thumbs up/thumbs down)."""
    if not _test_store.update_message_rating(team_id, message_id, req.rating.value):
        raise HTTPException(status_code=404, detail="Message not found")
    return {"message_id": message_id, "rating": req.rating.value}


@app.get("/teams/{team_id}/test-chat/quality-scores", response_model=List[AgentQualityScore])
def get_agent_quality_scores(team_id: str):
    """Get aggregated quality scores per agent based on chat ratings."""
    _get_team_or_404(team_id)
    rows = _test_store.get_agent_quality_scores(team_id)
    return [AgentQualityScore(**r) for r in rows]


# ---------------------------------------------------------------------------
# Pipeline Testing (End-to-End Walkthrough)
# ---------------------------------------------------------------------------


def _temporal_enabled() -> bool:
    """Return whether Temporal dispatch is active (``TEMPORAL_ADDRESS`` set).

    Preconditions: none.
    Postconditions: ``True`` iff ``shared.temporal`` is importable and reports Temporal
        enabled; ``False`` if Temporal is disabled or ``shared.temporal`` is absent (so
        the daemon-thread path is always reachable).
    """
    try:
        from shared.temporal import is_temporal_enabled
    except ImportError:
        return False
    return is_temporal_enabled()


def _dispatch_pipeline_run(
    run_id: str,
    team_agents: list[AgenticTeamAgent],
    process_def: ProcessDefinition,
    initial_input: Optional[str],
    *,
    temporal_owned: bool,
) -> str:
    """Dispatch a pipeline run via Temporal when enabled, else a daemon thread.

    Preconditions:
        - ``run_id`` refers to a run already created in the store with
          ``temporal_owned`` set to the same value passed here.
        - ``temporal_owned`` is the single ``_temporal_enabled()`` reading the caller
          used for the run's stored flag — computed once and passed in, never
          recomputed here, so the dispatch path can't diverge from what was persisted
          if Temporal availability changes between the two checks.

    Postconditions:
        - Starts exactly one execution path, selected by ``temporal_owned``, and
          returns its label ("Temporal" or "thread"). When true the run is started as
          a durable ``AgenticPipelineWorkflow``; otherwise the legacy daemon-thread
          path runs unchanged.
        - Any failure while starting the workflow propagates to the caller, which marks
          the run FAILED — a Temporal-enabled run is never silently downgraded.
    """
    if temporal_owned:
        from agent_team_studio.agentic_team_provisioning.temporal.start_workflow import (
            start_agentic_pipeline_workflow,
        )

        team_agents_json = [a.model_dump(mode="json") for a in team_agents]
        process_json = process_def.model_dump(mode="json")
        start_agentic_pipeline_workflow(run_id, team_agents_json, process_json, initial_input)
        return "Temporal"

    _pipeline_runner.start_run(run_id, team_agents, process_def)
    return "thread"


@app.post("/teams/{team_id}/test-pipeline/runs", response_model=TestPipelineRun, status_code=201)
def start_pipeline_run(team_id: str, req: StartPipelineRunRequest):
    """Start an end-to-end pipeline test run."""
    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    # Find the process
    process = None
    for p in team.processes:
        if p.process_id == req.process_id:
            process = p
            break
    if process is None:
        raise HTTPException(status_code=404, detail="Process not found")

    # Build ProcessDefinition from stored data
    process_def = (
        process if isinstance(process, ProcessDefinition) else ProcessDefinition(**process)
    )

    # Gather team agents
    team_agents_raw = _store.list_team_agents(team_id)
    team_agents = [
        a if isinstance(a, AgenticTeamAgent) else AgenticTeamAgent(**a) for a in team_agents_raw
    ]

    temporal_owned = _temporal_enabled()
    run_id = str(uuid.uuid4())
    run_row = _test_store.create_pipeline_run(
        run_id, team_id, req.process_id, req.initial_input, temporal_owned=temporal_owned
    )

    try:
        dispatch_method = _dispatch_pipeline_run(
            run_id, team_agents, process_def, req.initial_input, temporal_owned=temporal_owned
        )
    except Exception as exc:
        logger.exception("Failed to dispatch agentic pipeline run %s", run_id)
        _test_store.try_fail_pipeline_run(run_id, f"Dispatch failed: {exc}")
        raise HTTPException(status_code=500, detail="Failed to start pipeline run.") from exc
    logger.info("Agentic pipeline run %s dispatched via %s", run_id, dispatch_method)

    return TestPipelineRun(**run_row)


@app.get("/teams/{team_id}/test-pipeline/runs", response_model=List[TestPipelineRun])
def list_pipeline_runs(team_id: str):
    """List pipeline test runs for a team."""
    _get_team_or_404(team_id)
    rows = _test_store.list_pipeline_runs(team_id)
    return [TestPipelineRun(**r) for r in rows]


@app.get("/teams/{team_id}/test-pipeline/runs/{run_id}", response_model=TestPipelineRun)
def get_pipeline_run(team_id: str, run_id: str):
    """Get the current status and step results of a pipeline test run."""
    row = _test_store.get_pipeline_run(run_id)
    if not row or row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    return TestPipelineRun(**row)


@app.post("/teams/{team_id}/test-pipeline/runs/{run_id}/input")
def submit_pipeline_input(team_id: str, run_id: str, req: SubmitPipelineInputRequest):
    """Submit human input at a WAIT step to resume the pipeline."""
    row = _test_store.get_pipeline_run(run_id)
    if not row or row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    if row["status"] != "waiting_for_input":
        raise HTTPException(status_code=400, detail="Pipeline is not waiting for input")

    if _test_store.is_pipeline_run_temporal_owned(run_id):
        # Temporal-owned run. Do the authoritative resume transition synchronously here
        # (mirroring the thread path's compare-and-swap) BEFORE waking the workflow, then
        # deliver the answer as a signal:
        #   * The CAS flips waiting_for_input -> running and persists the input, so the
        #     /input contract holds — the response (and the caller's next poll) no longer
        #     shows waiting_for_input, and the same WAIT question is not re-surfaced.
        #   * Exactly one concurrent submit wins the CAS; a duplicate loses with a 409
        #     instead of a second signal overwriting the first answer.
        #   * A cancel that already moved the row terminal makes the CAS a no-op -> 409,
        #     so a resume can never revive a cancelled run.
        # The CAS durably records the resume; the workflow's wait_finalize_activity reads
        # the outcome from the store, so the signal below is only a best-effort *wake* to
        # resume promptly. If it fails (Temporal client down), the row is already
        # ``running`` with the input persisted, and the workflow reconciles it at the WAIT
        # timeout — so the run never gets stuck. We therefore do NOT 500 here (that would
        # contradict the already-committed running row); we log and return the updated row.
        from agent_team_studio.agentic_team_provisioning.temporal import WORKFLOW_ID_PREFIX
        from shared.temporal import signal_workflow_sync

        if not _test_store.try_resume_pipeline_run_temporal(run_id, req.input):
            raise HTTPException(
                status_code=409,
                detail="Pipeline run is no longer resumable (it timed out, was cancelled, "
                "or was reaped). Start a new run.",
            )
        try:
            signal_workflow_sync(f"{WORKFLOW_ID_PREFIX}{run_id}", "submit_input", req.input)
        except Exception:
            logger.warning(
                "Failed to signal agentic pipeline run %s; the resume is durably recorded "
                "and will be reconciled at the WAIT timeout",
                run_id,
                exc_info=True,
            )
        updated = _test_store.get_pipeline_run(run_id)
        return TestPipelineRun(**(updated or row))

    # The terminal transition is decided by a DB compare-and-swap, so this is race-free
    # even if the run timed out or was reaped between the read above and here — and it
    # works regardless of which worker owns the run's thread. A False return means the
    # run left waiting_for_input first (lost the race): report a conflict.
    if not _pipeline_runner.submit_human_input(run_id, req.input):
        raise HTTPException(
            status_code=409,
            detail="Pipeline run is no longer resumable (it timed out, was cancelled, "
            "or was reaped). Start a new run.",
        )
    updated = _test_store.get_pipeline_run(run_id)
    return TestPipelineRun(**(updated or row))


@app.post("/teams/{team_id}/test-pipeline/runs/{run_id}/cancel")
def cancel_pipeline_run(team_id: str, run_id: str):
    """Cancel a running or waiting pipeline test run."""
    row = _test_store.get_pipeline_run(run_id)
    if not row or row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    if row["status"] not in ("running", "waiting_for_input"):
        raise HTTPException(status_code=400, detail="Pipeline is not in a cancellable state")

    if _test_store.is_pipeline_run_temporal_owned(run_id):
        # Temporal-owned run: flip the store row first (immediate, consistent read for
        # this response) then request workflow cancellation — its cancel_reconcile
        # activity is then a CAS no-op. A cancel-signal failure is best-effort: the row
        # is already cancelled and the workflow will observe it out-of-band.
        from agent_team_studio.agentic_team_provisioning.temporal import WORKFLOW_ID_PREFIX
        from shared.temporal import cancel_workflow_sync

        _test_store.try_cancel_pipeline_run(run_id)
        try:
            cancel_workflow_sync(f"{WORKFLOW_ID_PREFIX}{run_id}")
        except Exception:
            logger.warning(
                "Failed to cancel agentic pipeline workflow for run %s", run_id, exc_info=True
            )
        updated = _test_store.get_pipeline_run(run_id)
        return TestPipelineRun(**(updated or row))

    _pipeline_runner.cancel_run(run_id)
    updated = _test_store.get_pipeline_run(run_id)
    return TestPipelineRun(**(updated or row))
