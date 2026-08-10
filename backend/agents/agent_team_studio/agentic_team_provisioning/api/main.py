"""FastAPI application for the Agentic Team Provisioning service.

This module is the app-assembly hub: it builds the app, owns the module-level
singletons and cross-domain collaborators, and mounts every extracted router.
All endpoint groups live in dedicated router/service pairs:

* ``api.routes.teams`` / ``api.services.teams`` — teams CRUD + roster
* ``api.routes.conversations`` / ``api.services.conversations`` — conversations
* ``api.routes.testing`` / ``api.services.testing`` — mode, test-chat, test-pipeline
* ``api.routes.processes`` / ``api.services.processes`` — process CRUD
* ``api.routes.jobs`` / ``api.services.jobs`` — team job status
* ``api.routes.questions`` / ``api.services.questions`` — pending questions
* ``api.routes.assets`` / ``api.services.assets`` — team assets (file system)
* ``api.routes.forms`` / ``api.services.forms`` — team form records (database)

Only the ``/health`` liveness probe stays defined directly on this module.

This module remains the owning namespace for collaborators the test suite
monkeypatches (``_store``, ``_agent``, ``_test_store``, ``_pipeline_runner``,
``_save_agents_from_llm``, ``_save_agents_and_process``, ``_after_process_saved``,
``_get_infra_or_404``, ``_get_team_or_404``, ``_roster_agent_from_manifest``,
``enrich_roster_agent``, ``resolve_persona``, ``_chat_context_agents``,
``_build_test_agent``, ``_call_test_agent``, …). Route and
service modules dereference those names through ``main`` at call time.
"""

from __future__ import annotations

import asyncio  # noqa: F401 — re-export: tests monkeypatch main.asyncio.to_thread
import logging
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError

from agent_team_studio.agentic_team_provisioning.agent_env_provisioning import (
    schedule_provision_step_agents,
)
from agent_team_studio.agentic_team_provisioning.assistant.agent import ProcessDesignerAgent
from agent_team_studio.agentic_team_provisioning.assistant.store import AgenticTeamStore
from agent_team_studio.agentic_team_provisioning.infrastructure import (
    TeamInfrastructure,
    get_team_infrastructure,
    provision_team,  # noqa: F401 — re-export: tests monkeypatch via main
)
from agent_team_studio.agentic_team_provisioning.manifest_generation import (
    manifest_agent_id,
    register_team_manifests,
)
from agent_team_studio.agentic_team_provisioning.models import (
    SOURCE_GENERATED,
    AgenticTeam,
    AgenticTeamAgent,
    EnrichedRosterAgent,
    ProcessDefinition,
)
from agent_team_studio.agentic_team_provisioning.postgres import SCHEMA as AGENTIC_POSTGRES_SCHEMA
from agent_team_studio.agentic_team_provisioning.roster_resolve import (
    EMPTY_ROSTER_PERSONA,
    llm_persona_lists_explicitly_empty,
    persona_tags_from_fat_raw,
    resolve_persona,
)
from agent_team_studio.agentic_team_provisioning.runtime.agent_builder import (
    build_agent as _build_test_agent,  # noqa: F401 — hub alias: services.testing + tests
)
from agent_team_studio.agentic_team_provisioning.runtime.agent_builder import (
    call_agent as _call_test_agent,  # noqa: F401 — hub alias: services.testing + tests
)
from agent_team_studio.agentic_team_provisioning.runtime.pipeline_runner import get_pipeline_runner
from agent_team_studio.agentic_team_provisioning.testing.store import get_test_store
from shared.app import create_team_app

logger = logging.getLogger(__name__)


def _startup() -> None:
    """Start the Temporal worker backstop, then run one-time service initialization.

    The team_service entrypoint normally starts the worker via
    ``TEAM_TEMPORAL_WORKER_MODULE`` before uvicorn accepts requests; this backstop
    covers running the app standalone (``uvicorn ...:app``).

    Preconditions:
        - None (safe to call once at app startup).

    Postconditions:
        - Starts the worker thread when Temporal is enabled; a no-op when
          ``TEMPORAL_ADDRESS`` is unset. Never raises — any failure is logged as a
          warning so it cannot abort app boot (this runs as an ``on_startup`` hook).
        - Calls :func:`initialize_service`, which performs retroactive team
          provisioning/registry registration and orphaned pipeline-run reaping.
          Also never raises (see its own contract).
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
    initialize_service()


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


def initialize_service() -> None:
    """Retroactive team provisioning/registry registration + orphaned-run reaping.

    Runs once at app startup (called from ``_startup``, the lifespan's
    ``on_startup`` hook) rather than at import time, so importing this module —
    e.g. for tests or tooling — never performs real database queries,
    infrastructure provisioning, or registry writes.

    Retroactive provisioning ensures all existing teams have infrastructure and
    that their generated agents are registered in the live registry (rosters are
    Postgres-backed, so this re-registers them after a process restart). Each team
    is isolated, and within a team infrastructure recovery and registry restoration
    are decoupled — a transient infrastructure failure must not hide an otherwise
    usable roster from the registry for the lifetime of the process.

    Restart cleanup: a pipeline test run whose worker thread died (restart or
    crash) leaves its DB row stuck in an active state with no live waiter. Reap
    orphans whose heartbeat has gone stale so they fail cleanly instead of
    stranding forever. Safe with multiple workers (advisory-locked,
    heartbeat-based) — a live sibling worker's run is never touched.

    Preconditions:
        - ``_store`` and ``_pipeline_runner`` are already constructed (they are
          module-level singletons, assigned above unconditionally).

    Postconditions:
        - Never raises: every failure mode (listing teams, provisioning a given
          team's infrastructure, registering its manifests, reaping orphaned
          runs) is caught and logged individually, so one team's or one step's
          failure cannot stop the others, and this function is safe to call as a
          best-effort startup backstop.
    """
    try:
        existing_teams = _store.list_teams()
    except Exception as exc:
        logger.warning("Could not list existing teams for retroactive provisioning: %s", exc)
        existing_teams = []

    for team_row in existing_teams:
        team_id = team_row["team_id"]
        try:
            get_team_infrastructure(team_id)
        except Exception as exc:
            logger.warning(
                "Could not retroactively provision infrastructure for team %s: %s", team_id, exc
            )
        try:
            team = _store.get_team(team_id)
            if team is not None and team.agents:
                register_team_manifests(team_id, team.agents)
        except Exception as exc:
            logger.warning("Could not register generated manifests for team %s: %s", team_id, exc)

    try:
        reaped = _pipeline_runner.reap_orphaned_runs()
        if reaped:
            logger.warning("Reaped %d orphaned pipeline run(s) on startup", reaped)
    except Exception as exc:
        logger.warning("Could not reap orphaned pipeline runs on startup: %s", exc)


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


def enrich_roster_agent(agent: AgenticTeamAgent) -> EnrichedRosterAgent:
    """Flatten a thin roster ref with persona fields from its linked manifest.

    Preconditions: ``agent`` is a valid ``AgenticTeamAgent``.
    Postconditions: returns an ``EnrichedRosterAgent`` with thin-ref fields intact.
        When ``agent.manifest_id`` resolves in the registry, persona fields equal
        ``resolve_persona(agent.manifest_id)``; when the manifest is missing, persona
        fields are empty (soft enrich — one orphan must not fail a list endpoint).
    """
    try:
        persona = resolve_persona(agent.manifest_id)
    except LookupError:
        persona = EMPTY_ROSTER_PERSONA
    return EnrichedRosterAgent(
        agent_name=agent.agent_name,
        source=agent.source,
        manifest_id=agent.manifest_id,
        **persona.model_dump(),
    )


def _chat_context_agents(team_id: str) -> list[dict[str, str]] | None:
    """Serialize roster agents for the process-designer LLM context.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: returns ``None`` when the roster is empty; otherwise a list of
        ``{"agent_name", "role"}`` dicts with ``role`` resolved from each agent's
        linked ``AgentManifest``.
    """
    agents = _store.list_team_agents(team_id)
    if not agents:
        return None
    out: list[dict[str, str]] = []
    for a in agents:
        enriched = enrich_roster_agent(a)
        out.append({"agent_name": enriched.agent_name, "role": enriched.role})
    return out


def _save_agents_from_llm(team_id: str, agents_data: list[dict[str, Any]] | None) -> None:
    """Persist the LLM ``agents`` block, preserving any registry-source roster entries.

    The chat round-trips only generated agents, so a naive full replace would drop
    the registry agents a user added via the from-registry endpoint (Agent Studio
    §5.3). We therefore merge: existing ``source == "registry"`` entries are kept, and
    the LLM's generated agents are layered on top — a generated agent that collides
    by name with a preserved registry agent is dropped, so the explicitly-added
    registry agent wins.

    Each LLM agent is stored as a thin ref (``manifest_id`` stamped) while its
    persona is written to the registry via ``register_team_manifests`` (summary from
    the LLM ``role`` field; free-text ``skills`` / ``tools`` / ``capabilities`` /
    ``expertise`` folded into Manifest skill tags — same mapping as legacy migrate).
    Explicit empty persona lists (e.g. ``"skills": []``) replace prior tags;
    omitted or whitespace-only lists preserve them.

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
    summaries: dict[str, str] = {}
    skill_tags: dict[str, list[str]] = {}
    for a in agents_data:
        name = a.get("agent_name", "")
        if not name:
            continue
        role = str(a.get("role") or "").strip()
        if role:
            summaries[name] = role
        # Fold skills/tools/capabilities/expertise into tags (migrate parity).
        # Non-blank tags replace prior Manifest tags. An explicitly empty list
        # (e.g. ``"skills": []`` with no non-blank persona tags) clears prior
        # tags. Absent / malformed persona keys, or whitespace-only values,
        # omit the skill_tags entry so register_team_manifests preserves prior
        # Manifest tags.
        raw = a if isinstance(a, dict) else {}
        folded = persona_tags_from_fat_raw(raw)
        if folded:
            skill_tags[name] = folded
        elif llm_persona_lists_explicitly_empty(raw):
            skill_tags[name] = []
        generated.append(
            AgenticTeamAgent(
                agent_name=name,
                source=SOURCE_GENERATED,
                manifest_id=manifest_agent_id(team_id, name),
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
        register_team_manifests(
            team_id, merged, summaries=summaries, skill_tags=skill_tags, conn=conn
        )

    # Merge under a team-row lock so the read (preserve registry agents), the write,
    # and the registry register all happen in one atomic, serialized transaction.
    _store.merge_generated_agents(team_id, generated, on_merged=_register)


def _after_process_saved(team_id: str, process: ProcessDefinition) -> None:
    """Provision per-step agent environments via agent_provisioning_team (background)."""
    schedule_provision_step_agents(team_id, process, _store)


def _save_agents_and_process(
    team_id: str,
    conversation_id: str,
    agents_data: list[dict[str, Any]] | None,
    process: ProcessDefinition | None,
) -> None:
    """Register a conversation turn's roster/process updates, or fail with a 503.

    Called by ``create_conversation``/``send_message`` after the LLM call and
    before either message of the turn is persisted, so a failure here still
    leaves the conversation history untouched (the caller lets this propagate
    before appending messages).

    Preconditions: ``team_id`` and ``conversation_id`` are guaranteed valid by
        the caller — ``create_conversation``/``send_message`` each look up the
        team/conversation before reaching this point — and are not
        independently re-validated here.
    Postconditions: on success, any entries in ``agents_data`` that carry an
        ``agent_name`` are merged into the roster (``_save_agents_from_llm``
        silently drops entries without one, and is a no-op if none remain),
        and ``process`` (if given) is saved and linked to the conversation.
        Not atomic across the two steps: if the roster save
        commits but the process save/link then fails, the roster is left
        updated while the process is not (documented gap, not silently
        hidden — closing it would need a shared transaction across
        ``_save_agents_from_llm`` and ``_store.save_process``, which the
        underlying stores don't currently support). A client retry after
        such a failure re-invokes the LLM and calls this helper again, but
        that cannot duplicate roster entries: ``merge_generated_agents``
        fully replaces the generated-sourced portion of the roster on every
        call (only ``source == "registry"`` entries survive from what was
        already there), so re-merging is idempotent with respect to the
        generated set, not additive. Scheduling background
        agent-env provisioning (``_after_process_saved``) is best-effort: it
        runs after the process is already saved and linked, and a failure
        there is logged and swallowed rather than propagated, so it can never
        discard an otherwise-successful turn or make the roster/process saves
        look retryable when they already committed. On failure of the roster
        or process save itself, a non-retryable data/programming error
        (``ValueError``, ``TypeError``, ``AttributeError``, ``KeyError``,
        pydantic ``ValidationError`` — e.g. malformed LLM ``agents_data``)
        propagates as itself, since retrying an identical malformed request
        cannot succeed. An ``HTTPException`` already raised by a callee also
        propagates unchanged (it is an intentional status, not an outage).
        Any other failure (e.g. a registry or database outage) is raised as
        ``HTTPException(503)`` instead of the underlying exception, so the
        caller gets a clear "try again" signal.
    """
    try:
        _save_agents_from_llm(team_id, agents_data)
        if process:
            _store.save_process(team_id, process)
            _store.set_conversation_process(conversation_id, process.process_id)
            try:
                _after_process_saved(team_id, process)
            except Exception:
                # Best-effort: scheduling provisioning must not discard an
                # already-committed roster/process, nor the turn about to be
                # persisted by the caller.
                logger.exception(
                    "Failed to schedule background agent-env provisioning for "
                    "team %s process %s; roster/process already saved, "
                    "continuing",
                    team_id,
                    process.process_id,
                )
    except (ValueError, TypeError, AttributeError, KeyError, ValidationError):
        # Not retryable: a data-shape or programming bug, not a transient
        # outage — let it propagate as itself (500) rather than a misleading
        # "service unavailable, try again".
        logger.exception(
            "Non-retryable error saving roster/process for team %s (conversation %s)",
            team_id,
            conversation_id,
        )
        raise
    except HTTPException:
        # An intentionally raised HTTP status (e.g. a client error) is neither
        # of the two categories above — propagate it unchanged rather than
        # relabeling it a retryable 503.
        raise
    except Exception as exc:
        logger.exception(
            "Failed to save roster/process for team %s (conversation %s); "
            "turn discarded, no messages persisted",
            team_id,
            conversation_id,
        )
        raise HTTPException(
            status_code=503,
            detail="Failed to update the team roster or process; please try again.",
        ) from exc


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    """Liveness probe.

    Preconditions: none.
    Postconditions: ``200`` with a static ``{"status": "ok", ...}`` body — never
        touches the store or infra, so it can't fail on their behalf.
    """
    return {"status": "ok", "service": "agentic-team-provisioning"}


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


# --- Mount extracted routers last (hub + globals already defined) ---
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    assets as assets_routes,
)
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    conversations as conversations_routes,
)
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    forms as forms_routes,
)
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    jobs as jobs_routes,
)
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    processes as processes_routes,
)
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    questions as questions_routes,
)
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    teams as teams_routes,
)
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    testing as testing_routes,
)
from agent_team_studio.agentic_team_provisioning.api.services.assets import (  # noqa: E402,F401
    _ASSET_UPLOAD_CHUNK_BYTES,  # re-export: tests monkeypatch via main
    _safe_asset_name,  # re-export: tests call directly via main
)
from agent_team_studio.agentic_team_provisioning.api.services.teams import (  # noqa: E402,F401
    _roster_agent_from_manifest,  # re-export: tests import + monkeypatch via main
)
from agent_team_studio.agentic_team_provisioning.api.services.testing import (  # noqa: E402,F401
    _dispatch_pipeline_run,  # re-export: hub call surface
    _find_agent_in_roster,  # re-export: tests monkeypatch via main
    _temporal_enabled,  # re-export: tests monkeypatch via main
)

_teams_router = teams_routes.router
_conversations_router = conversations_routes.router
_testing_router = testing_routes.router
_processes_router = processes_routes.router
_jobs_router = jobs_routes.router
_questions_router = questions_routes.router
_assets_router = assets_routes.router
_forms_router = forms_routes.router
app.include_router(_teams_router)
app.include_router(_conversations_router)
app.include_router(_testing_router)
app.include_router(_processes_router)
app.include_router(_jobs_router)
app.include_router(_questions_router)
app.include_router(_assets_router)
app.include_router(_forms_router)
