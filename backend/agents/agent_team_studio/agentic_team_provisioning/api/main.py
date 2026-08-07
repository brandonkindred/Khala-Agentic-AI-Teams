"""FastAPI application for the Agentic Team Provisioning service.

This module is the app-assembly hub. Extracted concerns live in:

* ``api.routes.teams`` / ``api.services.teams`` — teams CRUD + roster
* ``api.routes.conversations`` / ``api.services.conversations`` — conversations
* ``api.routes.testing`` / ``api.services.testing`` — mode, test-chat, test-pipeline

Remaining endpoint groups (processes, jobs, questions, assets, forms) still
live here pending later splits.

This module remains the owning namespace for collaborators the test suite
monkeypatches (``_store``, ``_agent``, ``_test_store``, ``_pipeline_runner``,
``_save_agents_from_llm``, ``_roster_agent_from_manifest``, …). Route and
service modules dereference those names through ``main`` at call time.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List
from urllib.parse import quote

from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse

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
from agent_team_studio.agentic_team_provisioning.manifest_generation import register_team_manifests
from agent_team_studio.agentic_team_provisioning.models import (
    AgentEnvProvisionSummary,
    AgenticTeam,
    AgenticTeamAgent,
    AssetInfo,
    CreateFormRecordRequest,
    FormRecord,
    ProcessDefinition,
    ProcessOutput,
    ProcessStatus,
    ProcessTrigger,
    RecommendAgentsResponse,
    RecommendedAgent,
    SubmitTeamAnswersRequest,
    TeamJobDetail,
    TeamJobSummary,
    TeamPendingQuestion,
    UpdateFormRecordRequest,
)
from agent_team_studio.agentic_team_provisioning.postgres import SCHEMA as AGENTIC_POSTGRES_SCHEMA
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


# --- Mount extracted routers last (hub + globals already defined) ---
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    conversations as conversations_routes,
)
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    teams as teams_routes,
)
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    testing as testing_routes,
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
app.include_router(_teams_router)
app.include_router(_conversations_router)
app.include_router(_testing_router)
