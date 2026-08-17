"""User Agent Founder API — autonomous startup founder driving the SE team."""

from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import HTTPException, Response
from pydantic import BaseModel, Field

from agent_team_studio.user_agent_founder.agent import FounderAgent
from agent_team_studio.user_agent_founder.orchestrator import run_workflow
from agent_team_studio.user_agent_founder.postgres import (
    SCHEMA as USER_AGENT_FOUNDER_POSTGRES_SCHEMA,
)
from agent_team_studio.user_agent_founder.store import (
    DEFAULT_TARGET_TEAM_KEY,
    StoredPersona,
    get_founder_store,
    get_persona_store,
)
from agent_team_studio.user_agent_founder.targets import ADAPTERS, AGENTIC_TEAM_PREFIX, get_adapter
from agent_team_studio.user_agent_founder.targets.agentic_team import (
    PROVISIONING_PREFIX,
    UNIFIED_API_BASE,
)
from shared.app import create_team_app

logger = logging.getLogger(__name__)


def _startup() -> None:
    """Seed builtin personas and start the Temporal worker backstop (best-effort).

    Both steps log-and-continue on failure. The Temporal start is a backstop for
    running this app standalone (`uvicorn ...:app`): the team_service entrypoint
    normally starts the worker via TEAM_TEMPORAL_WORKER_MODULE first; the start
    helper is idempotent and a no-op when TEMPORAL_ADDRESS is unset.
    """
    try:
        inserted = get_persona_store().seed_builtins()
        logger.info(
            "persona startup-founder %s",
            "seeded (builtin)" if inserted else "already present",
        )
    except Exception:
        logger.exception("user_agent_founder persona seeding failed")
    try:
        from agent_team_studio.user_agent_founder.temporal.worker import (
            start_user_agent_founder_temporal_worker_thread,
        )

        start_user_agent_founder_temporal_worker_thread()
    except Exception:
        logger.warning(
            "user_agent_founder Temporal worker start (lifespan backstop) failed",
            exc_info=True,
        )


app = create_team_app(
    service_name="user-agent-founder",
    team_key="user_agent_founder",
    title="User Agent Founder API",
    description=(
        "Autonomous startup founder agent that generates a product spec, "
        "submits it to the Software Engineering team, and answers all questions "
        "through the lens of a budget-conscious, speed-first, UX-obsessed founder."
    ),
    version="1.0.0",
    postgres_schema=USER_AGENT_FOUNDER_POSTGRES_SCHEMA,
    on_startup=_startup,
)

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


DEFAULT_PERSONA_ID = "startup-founder"


class StartRunRequest(BaseModel):
    persona_id: str = Field(
        default=DEFAULT_PERSONA_ID,
        description="Which persona drives this run. Must exist in PersonaStore.",
    )
    target_team_key: str = Field(
        default=DEFAULT_TARGET_TEAM_KEY,
        description="Which target team this persona run drives. Must be a key in targets.ADAPTERS.",
    )
    project_name: Optional[str] = Field(
        default=None,
        description=(
            "Optional project slug for the target team. When omitted, computed "
            "server-side as '<slug(persona_name)>-<run_id[:8]>' to guarantee "
            "uniqueness across repeat runs."
        ),
    )
    process_id: Optional[str] = Field(
        default=None,
        description=(
            "Process the persona should drive, for agentic-team targets "
            "(target_team_key='agentic_team:<id>'). The AgenticTeamAdapter runs "
            "this process via the team's test-pipeline. Ignored by the "
            "software-engineering target, which has no process concept."
        ),
    )


class StartRunResponse(BaseModel):
    # External-facing key used by the team-assistant launch endpoint and the
    # jobs UI. Internally the team still uses ``run_id`` for its own rows;
    # we only rename at the wire.
    job_id: str
    status: str = "pending"
    message: str = "Founder workflow started. Poll GET /status/{job_id} for progress."


class DecisionResponse(BaseModel):
    decision_id: int
    question_id: str
    question_text: str
    answer_text: str
    rationale: str
    timestamp: str


class RunStatusResponse(BaseModel):
    run_id: str
    status: str
    se_job_id: Optional[str] = None
    analysis_job_id: Optional[str] = None
    spec_content: Optional[str] = None
    repo_path: Optional[str] = None
    target_team_key: str = DEFAULT_TARGET_TEAM_KEY
    persona_id: Optional[str] = None
    project_name: Optional[str] = None
    process_id: Optional[str] = None
    created_at: str
    updated_at: str
    error: Optional[str] = None
    decisions: list[DecisionResponse] = Field(default_factory=list)


class RunSummaryResponse(BaseModel):
    run_id: str
    status: str
    se_job_id: Optional[str] = None
    analysis_job_id: Optional[str] = None
    target_team_key: str = DEFAULT_TARGET_TEAM_KEY
    persona_id: Optional[str] = None
    project_name: Optional[str] = None
    created_at: str
    updated_at: str
    error: Optional[str] = None


class RunListResponse(BaseModel):
    runs: list[RunSummaryResponse]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _build_agent_for_run(run_id: str) -> FounderAgent:
    """Construct a FounderAgent using the persona prompts associated with a run.

    Resolves the persona via the run row's ``persona_id``. Falls back to
    the bundled default prompts if either the run or the persona row is
    missing — keeps thread-mode dispatch resilient against orphaned rows
    while still picking up custom voices when present.
    """
    store = get_founder_store()
    run = store.get_run(run_id)
    if run is None or not run.persona_id:
        return FounderAgent()
    persona = get_persona_store().get_persona(run.persona_id)
    if persona is None:
        return FounderAgent()
    return FounderAgent(
        system_prompt=persona.system_prompt,
        spec_generation_prompt=persona.spec_generation_prompt,
    )


def _dispatch_founder_run(run_id: str) -> str:
    """Dispatch a founder run via Temporal when enabled, else a daemon thread.

    Returns a short label describing which execution mode was used so the
    caller can include it in the response message. Raises ``RuntimeError``
    if Temporal is enabled but the workflow fails to start.
    """
    try:
        from shared.temporal import is_temporal_enabled

        if is_temporal_enabled():
            from agent_team_studio.user_agent_founder.temporal.start_workflow import (
                start_founder_workflow,
            )

            start_founder_workflow(run_id)
            logger.info("Founder workflow started via Temporal: run_id=%s", run_id)
            return "Temporal"
    except ImportError:
        pass

    store = get_founder_store()
    agent = _build_agent_for_run(run_id)
    run = store.get_run(run_id)
    team_key = (run.target_team_key if run is not None else None) or DEFAULT_TARGET_TEAM_KEY
    # Thread the run's process_id (and spec, for the resume window) into the
    # adapter: this path passes a non-None adapter to run_workflow, so its own
    # construction fallback never runs — an agentic run would otherwise reach
    # start_build with process_id=None.
    adapter = get_adapter(
        team_key,
        process_id=run.process_id if run is not None else None,
        spec=run.spec_content if run is not None else None,
    )
    thread = threading.Thread(
        target=run_workflow,
        args=(run_id, store, agent, adapter),
        name=f"founder-workflow-{run_id[:8]}",
        daemon=True,
    )
    thread.start()
    logger.info("Founder workflow thread started: run_id=%s", run_id)
    return "thread"


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify_persona_name(name: str, *, max_len: int = 32) -> str:
    """Lowercase, hyphen-separated, ``[a-z0-9-]`` only. Falls back to 'persona'."""
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    slug = slug[:max_len].rstrip("-")
    return slug or "persona"


def _persona_to_info(p: StoredPersona) -> "PersonaInfo":
    return PersonaInfo(
        id=p.persona_id,
        name=p.name,
        description=p.description,
        icon=p.icon,
        is_builtin=p.is_builtin,
        system_prompt=p.system_prompt,
        spec_generation_prompt=p.spec_generation_prompt,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


@app.post("/start", response_model=StartRunResponse)
def start_founder_workflow(
    request: Optional[StartRunRequest] = None,
) -> StartRunResponse:
    """Kick off the autonomous founder workflow.

    The agent will:
    1. Generate a task management product spec
    2. Submit it to the target team for product analysis
    3. Answer all target-team questions autonomously
    4. Trigger the full target-team build pipeline

    The run is registered with the centralized job service **before**
    dispatch so it appears in the Jobs Dashboard immediately.
    """
    from agent_team_studio.user_agent_founder.shared import job_store

    req = request or StartRunRequest()
    # Validate up-front so an unknown key returns 400 instead of crashing the
    # background dispatch thread later. ``get_adapter`` parses agentic-team keys
    # ("agentic_team:<id>") as well as the static registry keys.
    try:
        get_adapter(req.target_team_key, process_id=req.process_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # An agentic-team run drives a specific process; enforce the Stage-3 → Stage-4
    # gate server-side (not just in the UI) so a direct caller or a stale handoff
    # can't start a persona test against a missing/draft/archived process.
    if req.target_team_key.startswith(AGENTIC_TEAM_PREFIX):
        if not req.process_id or not req.process_id.strip():
            raise HTTPException(
                status_code=400,
                detail="process_id is required when target_team_key is an agentic team",
            )
        team_id = req.target_team_key[len(AGENTIC_TEAM_PREFIX) :]
        status = _agentic_process_status(team_id, req.process_id)
        # ``None`` means the provisioning service was unreachable: stay best-effort
        # and allow (an outage must not hard-block starts; a truly unrunnable
        # process surfaces as a run failure). A *known* non-complete status is a
        # gate violation and is rejected.
        if status is not None and status != "complete":
            detail = (
                f"team {team_id} or process {req.process_id} not found"
                if status == "not_found"
                else f"process {req.process_id} is not testable (status: {status})"
            )
            raise HTTPException(status_code=422, detail=detail)

    persona = get_persona_store().get_persona(req.persona_id)
    if persona is None:
        raise HTTPException(status_code=404, detail=f"Persona {req.persona_id!r} not found")

    store = get_founder_store()
    run_id = uuid4().hex
    project_name = req.project_name or (f"{_slugify_persona_name(persona.name)}-{run_id[:8]}")
    store.create_run(
        target_team_key=req.target_team_key,
        run_id=run_id,
        persona_id=persona.persona_id,
        project_name=project_name,
        process_id=req.process_id,
    )

    job_store.create_job(
        run_id,
        status=job_store.JOB_STATUS_RUNNING,
        label="Testing Personas workflow",
        current_phase="starting",
    )

    try:
        mode = _dispatch_founder_run(run_id)
    except Exception as exc:
        logger.exception("Failed to dispatch founder workflow for %s", run_id)
        job_store.update_job(
            run_id, status=job_store.JOB_STATUS_FAILED, error=f"Dispatch failed: {exc}"
        )
        store.update_run(run_id, status="failed", error=f"Dispatch failed: {exc}")
        # Full exception logged above; return a generic detail so internal
        # exception text isn't disclosed to the API client.
        raise HTTPException(status_code=500, detail="Failed to start workflow.") from exc

    return StartRunResponse(
        job_id=run_id,
        status=job_store.JOB_STATUS_RUNNING,
        message=f"Founder workflow started ({mode}). Poll GET /status/{run_id} for progress.",
    )


@app.get("/status/{run_id}", response_model=RunStatusResponse)
def get_run_status(run_id: str) -> RunStatusResponse:
    """Get the current status of a founder workflow run, including all decisions made."""
    store = get_founder_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    decisions = store.get_decisions(run_id)
    return RunStatusResponse(
        run_id=run.run_id,
        status=run.status,
        se_job_id=run.se_job_id,
        analysis_job_id=run.analysis_job_id,
        spec_content=run.spec_content,
        repo_path=run.repo_path,
        target_team_key=run.target_team_key,
        persona_id=run.persona_id,
        project_name=run.project_name,
        process_id=run.process_id,
        created_at=run.created_at,
        updated_at=run.updated_at,
        error=run.error,
        decisions=[
            DecisionResponse(
                decision_id=d.decision_id,
                question_id=d.question_id,
                question_text=d.question_text,
                answer_text=d.answer_text,
                rationale=d.rationale,
                timestamp=d.timestamp,
            )
            for d in decisions
        ],
    )


@app.get("/runs", response_model=RunListResponse)
def list_runs() -> RunListResponse:
    """List all founder workflow runs."""
    store = get_founder_store()
    runs = store.list_runs()
    return RunListResponse(
        runs=[
            RunSummaryResponse(
                run_id=r.run_id,
                status=r.status,
                se_job_id=r.se_job_id,
                analysis_job_id=r.analysis_job_id,
                target_team_key=r.target_team_key,
                persona_id=r.persona_id,
                project_name=r.project_name,
                created_at=r.created_at,
                updated_at=r.updated_at,
                error=r.error,
            )
            for r in runs
        ]
    )


@app.get("/decisions/{run_id}", response_model=list[DecisionResponse])
def get_decisions(run_id: str) -> list[DecisionResponse]:
    """Get all decisions and rationale for a specific run."""
    store = get_founder_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    decisions = store.get_decisions(run_id)
    return [
        DecisionResponse(
            decision_id=d.decision_id,
            question_id=d.question_id,
            question_text=d.question_text,
            answer_text=d.answer_text,
            rationale=d.rationale,
            timestamp=d.timestamp,
        )
        for d in decisions
    ]


class PersonaInfo(BaseModel):
    id: str
    name: str
    description: str
    icon: str
    is_builtin: bool = False
    system_prompt: str = ""
    spec_generation_prompt: str = ""
    created_at: str = ""
    updated_at: str = ""


class PersonaListResponse(BaseModel):
    personas: list[PersonaInfo]


class CreatePersonaRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field(..., min_length=1, max_length=2000)
    icon: str = Field("person", min_length=1, max_length=64)
    system_prompt: str = Field(..., min_length=1)
    spec_generation_prompt: str = Field(..., min_length=1)


class UpdatePersonaRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    description: Optional[str] = Field(None, min_length=1, max_length=2000)
    icon: Optional[str] = Field(None, min_length=1, max_length=64)
    system_prompt: Optional[str] = Field(None, min_length=1)
    spec_generation_prompt: Optional[str] = Field(None, min_length=1)


class TestableTeam(BaseModel):
    team_key: str
    display_name: str


class TestableTeamsResponse(BaseModel):
    teams: list[TestableTeam]


class ChatMessageResponse(BaseModel):
    message_id: int
    role: str
    content: str
    message_type: str
    metadata: Optional[dict[str, Any]] = None
    timestamp: str


class ChatHistoryResponse(BaseModel):
    run_id: str
    messages: list[ChatMessageResponse]


class SendChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


class RunArtifactsResponse(BaseModel):
    run_id: str
    se_job_id: Optional[str] = None
    se_job_status: Optional[dict[str, Any]] = None
    repo_path: Optional[str] = None
    spec_content: Optional[str] = None


@app.get("/personas", response_model=PersonaListResponse)
def list_personas() -> PersonaListResponse:
    """Return all personas available for testing other teams."""
    personas = get_persona_store().list_personas()
    return PersonaListResponse(personas=[_persona_to_info(p) for p in personas])


@app.post("/personas", response_model=PersonaInfo, status_code=201)
def create_persona(request: CreatePersonaRequest) -> PersonaInfo:
    persona = get_persona_store().create_persona(
        name=request.name,
        description=request.description,
        icon=request.icon,
        system_prompt=request.system_prompt,
        spec_generation_prompt=request.spec_generation_prompt,
    )
    return _persona_to_info(persona)


@app.get("/personas/{persona_id}", response_model=PersonaInfo)
def get_persona(persona_id: str) -> PersonaInfo:
    persona = get_persona_store().get_persona(persona_id)
    if persona is None:
        raise HTTPException(status_code=404, detail=f"Persona {persona_id!r} not found")
    return _persona_to_info(persona)


@app.put("/personas/{persona_id}", response_model=PersonaInfo)
def update_persona(persona_id: str, request: UpdatePersonaRequest) -> PersonaInfo:
    """Edit a persona. Built-in personas are editable like any other row."""
    updates = request.model_dump(exclude_unset=True, exclude_none=True)
    if not updates:
        # No-op edit: return the current row instead of issuing a UPDATE.
        existing = get_persona_store().get_persona(persona_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"Persona {persona_id!r} not found")
        return _persona_to_info(existing)
    persona = get_persona_store().update_persona(persona_id, **updates)
    if persona is None:
        raise HTTPException(status_code=404, detail=f"Persona {persona_id!r} not found")
    return _persona_to_info(persona)


@app.delete("/personas/{persona_id}", status_code=204, response_class=Response)
def delete_persona(persona_id: str) -> Response:
    """Delete a persona. Built-ins are deletable; the seed will recreate them on restart."""
    if not get_persona_store().delete_persona(persona_id):
        raise HTTPException(status_code=404, detail=f"Persona {persona_id!r} not found")
    return Response(status_code=204)


# These cross-service checks run **inline** on ``/start`` and ``/testable-teams``,
# so they block the request thread. They are best-effort (a slow/unresponsive
# provisioning service must not hard-block), so the timeout is kept tight to bound
# the worst-case added latency rather than the 30s used on the founder's own build
# calls. 5s total / 3s connect: long enough to ride out a brief hiccup, short
# enough that a dead provisioning service degrades the endpoint by seconds, not
# tens of seconds.
_BEST_EFFORT_TIMEOUT = httpx.Timeout(5.0, connect=3.0)


def _provisioning_base() -> str:
    """Base URL for the agentic-team-provisioning service over the unified API."""
    # rstrip so a trailing-slash env value can't produce ``//api/...``.
    return f"{UNIFIED_API_BASE.rstrip('/')}{PROVISIONING_PREFIX}"


def _fetch_agentic_team(client: httpx.Client, team_id: str) -> tuple[int, dict]:
    """GET one agentic team's detail. Returns ``(status_code, team_dict)``.

    Preconditions: ``client`` is an open httpx client.
    Postconditions: ``team_dict`` is the response's ``.team`` object on a 2xx
        (``{}`` if absent), and ``{}`` on an HTTP error. ``team_id`` is
        percent-encoded into the path (defense-in-depth against traversal even
        though callers validate it). Propagates transport exceptions to the
        caller (each caller decides how to degrade).
    """
    resp = client.get(f"{_provisioning_base()}/teams/{quote(team_id, safe='')}")
    if resp.status_code >= 400:
        return resp.status_code, {}
    data = resp.json()
    if not isinstance(data, dict):  # a list/scalar body has no .get("team")
        return resp.status_code, {}
    team = data.get("team")
    # Enforce the documented dict contract: a truthy *non-dict* ``team`` (e.g. a
    # list from an API shape change) would otherwise be returned verbatim and
    # AttributeError in callers that do ``team.get(...)``.
    return resp.status_code, team if isinstance(team, dict) else {}


def _agentic_process_status(team_id: str, process_id: str) -> Optional[str]:
    """Return the status of ``process_id`` on an agentic team, cross-service.

    Postconditions: returns the process's ``status`` string (e.g. ``"complete"``,
        ``"draft"``, ``"archived"``) when the team+process resolve; the synthetic
        ``"not_found"`` (distinct from any real ``process_status`` value) when the
        team is definitively **not found** (``404``) or the process isn't on it;
        and ``None`` when the status can't be determined — a transport failure
        **or** a non-404 HTTP error (``5xx``, auth ``401/403``, rate-limit) —
        which the caller treats as "cannot determine" and must not hard-block on
        (best-effort). A transient ``503`` is thus an outage, not a gate
        violation. Never raises.
    """
    try:
        with httpx.Client(timeout=_BEST_EFFORT_TIMEOUT) as client:
            code, team = _fetch_agentic_team(client, team_id)
        if code == 404:
            return "not_found"  # definitively not found ⇒ a real gate rejection
        if code >= 400:
            return None  # 5xx/auth/transient ⇒ undeterminable, don't hard-block
        for proc in team.get("processes") or []:
            if proc.get("process_id") == process_id:
                return proc.get("status") or "unknown"
        return "not_found"
    except Exception:
        logger.warning(
            "Could not verify agentic process status for team %s / process %s",
            team_id,
            process_id,
            exc_info=True,
        )
        return None


def _list_agentic_testable_teams() -> list[TestableTeam]:
    """Enumerate agentic teams that have at least one ``complete`` process.

    Cross-service, best-effort: queries the agentic-team-provisioning service
    over the unified API. ``GET /teams`` is expected to return a **JSON list** of
    team summaries (``{team_id, name, process_count, ...}``); any other shape
    (e.g. an object envelope) is logged and treated as empty. The "≥1 complete
    process" filter is applied here, server-side, so the dropdown the frontend
    consumes is ready to use and the Stage-3 → Stage-4 gate (a complete process
    is required to test) is honored.

    Postconditions: returns one :class:`TestableTeam` per agentic team with a
        ``complete`` process, keyed ``"agentic_team:<team_id>"``, in the order the
        ``/teams`` list returned them. Best-effort and **partial-failure
        tolerant**: a per-team detail fetch that errors or raises skips only that
        team (the others are kept), and a top-level failure (e.g. the list call
        itself) returns ``[]``. The per-team detail fetches — the N+1 hot spot —
        run concurrently in a bounded thread pool to keep total latency near a
        single round-trip rather than the sum of all of them. Failures are
        logged, never raised.
    """
    try:
        with httpx.Client(timeout=_BEST_EFFORT_TIMEOUT) as client:
            resp = client.get(f"{_provisioning_base()}/teams")
            if resp.status_code >= 400:
                logger.warning("Could not list agentic teams: HTTP %s", resp.status_code)
                return []
            summaries = resp.json()
            if not isinstance(summaries, list):
                logger.warning(
                    "Unexpected /teams response shape (%s); skipping agentic teams",
                    type(summaries).__name__,
                )
                return []
            # Candidates: a real team_id whose summary doesn't *explicitly* report
            # zero processes. A missing/None ``process_count`` is not a reliable
            # "no processes" signal, so keep it and let the detail check decide.
            # Filter to dicts first so a single non-dict element (e.g. a stray
            # string) can't raise AttributeError on ``.get`` and discard *every*
            # valid team via the outer except.
            candidates = [
                s
                for s in summaries
                if isinstance(s, dict) and s.get("team_id") and s.get("process_count") != 0
            ]
            if not candidates:
                return []

            def _eligible(summary: dict) -> "TestableTeam | None":
                team_id = summary["team_id"]
                # Guard each detail fetch so one flaky/timing-out team doesn't
                # discard the others (it contributes None, which is filtered out).
                try:
                    code, team = _fetch_agentic_team(client, team_id)
                except Exception:
                    logger.warning(
                        "Could not fetch agentic team %s detail; skipping", team_id, exc_info=True
                    )
                    return None
                if code >= 400:
                    return None
                processes = team.get("processes") or []
                if any(p.get("status") == "complete" for p in processes):
                    return TestableTeam(
                        team_key=f"agentic_team:{team_id}",
                        display_name=team.get("name") or summary.get("name") or team_id,
                    )
                return None

            # httpx.Client is thread-safe for concurrent requests; ``pool.map``
            # preserves input order so the dropdown stays stable.
            with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
                results = list(pool.map(_eligible, candidates))
            return [t for t in results if t is not None]
    except Exception:
        logger.warning("Could not enumerate agentic testable teams", exc_info=True)
        return []


@app.get("/testable-teams", response_model=TestableTeamsResponse)
def list_testable_teams() -> TestableTeamsResponse:
    """List the target teams a persona can test.

    Combines the static registry targets (``targets.ADAPTERS``, e.g. Software
    Engineering) with the dynamic agentic teams that have a ``complete`` process
    (:func:`_list_agentic_testable_teams`). The agentic-team filter is applied
    server-side so the frontend does no filtering.
    """
    try:
        from unified_api.config import TEAM_CONFIGS
    except Exception:
        logger.warning("Could not import TEAM_CONFIGS; using default display names", exc_info=True)
        TEAM_CONFIGS = {}
    # Guard the shape too: if the import succeeds but TEAM_CONFIGS isn't a dict
    # (e.g. a module/None after a refactor), the ``.get`` below would 500. Degrade
    # to generated display names instead.
    if not isinstance(TEAM_CONFIGS, dict):
        logger.warning(
            "TEAM_CONFIGS is not a dict (%s); using default display names",
            type(TEAM_CONFIGS).__name__,
        )
        TEAM_CONFIGS = {}
    teams: list[TestableTeam] = []
    for team_key in ADAPTERS:
        cfg = TEAM_CONFIGS.get(team_key)
        # getattr (not ``cfg.name``) so an unexpected config object shape degrades
        # to the generated display name instead of crashing the endpoint.
        display_name = getattr(cfg, "name", None) or team_key.replace("_", " ").title()
        teams.append(TestableTeam(team_key=team_key, display_name=display_name))
    teams.extend(_list_agentic_testable_teams())
    return TestableTeamsResponse(teams=teams)


@app.get("/runs/{run_id}/artifacts", response_model=RunArtifactsResponse)
def get_run_artifacts(run_id: str) -> RunArtifactsResponse:
    """Get artifacts produced during a persona test run.

    Proxies to the target-team job status (resolved via the run's
    ``target_team_key``) to retrieve task results, task states, and
    other pipeline outputs.
    """
    store = get_founder_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    se_job_status: dict[str, Any] | None = None
    if run.se_job_id:
        try:
            adapter = get_adapter(run.target_team_key)
        except ValueError:
            logger.warning(
                "Unknown target_team_key %r on run %s; cannot fetch artifacts",
                run.target_team_key,
                run.run_id,
            )
        else:
            try:
                with httpx.Client() as client:
                    payload = adapter.poll_build(client, run.se_job_id)
                if not payload.get("_poll_error"):
                    se_job_status = payload
            except httpx.HTTPError:
                logger.warning("Failed to fetch target-team job status for %s", run.se_job_id)

    return RunArtifactsResponse(
        run_id=run.run_id,
        se_job_id=run.se_job_id,
        se_job_status=se_job_status,
        repo_path=run.repo_path,
        spec_content=run.spec_content,
    )


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


@app.get("/runs/{run_id}/chat", response_model=ChatHistoryResponse)
def get_chat_history(run_id: str, since_id: int = 0) -> ChatHistoryResponse:
    """Get chat messages for a run, optionally only messages after since_id."""
    store = get_founder_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    messages = store.get_chat_messages(run_id, since_id=since_id)
    return ChatHistoryResponse(
        run_id=run_id,
        messages=[
            ChatMessageResponse(
                message_id=m.message_id,
                role=m.role,
                content=m.content,
                message_type=m.message_type,
                metadata=m.metadata,
                timestamp=m.timestamp,
            )
            for m in messages
        ],
    )


@app.post("/runs/{run_id}/chat", response_model=ChatHistoryResponse)
def send_chat_message(run_id: str, request: SendChatRequest) -> ChatHistoryResponse:
    """Send a message to the founder persona and get a response."""
    store = get_founder_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    # Store user message
    store.add_chat_message(run_id, "user", request.message, "chat")

    # Build context for the persona
    decisions = store.get_decisions(run_id)
    context: dict[str, Any] = {
        "status": run.status,
        "recent_decisions": [
            {"question_text": d.question_text, "answer_text": d.answer_text} for d in decisions[-5:]
        ],
    }

    # Get persona response
    agent = _build_agent_for_run(run_id)
    try:
        response = agent.chat(request.message, context)
    except Exception:
        logger.exception("Chat LLM call failed for run %s", run_id)
        # Full exception logged above; don't surface internal exception text to the user.
        response = "Sorry, I'm having trouble responding right now. Please try again."

    store.add_chat_message(run_id, "assistant", response, "chat")

    # Return recent messages
    messages = store.get_chat_messages(run_id)
    return ChatHistoryResponse(
        run_id=run_id,
        messages=[
            ChatMessageResponse(
                message_id=m.message_id,
                role=m.role,
                content=m.content,
                message_type=m.message_type,
                metadata=m.metadata,
                timestamp=m.timestamp,
            )
            for m in messages
        ],
    )


# ---------------------------------------------------------------------------
# Job management (centralized job service integration for Jobs Dashboard)
# ---------------------------------------------------------------------------


class FounderJobSummary(BaseModel):
    job_id: str
    status: str
    label: str = "Persona: founder workflow"
    current_phase: Optional[str] = None
    created_at: Optional[str] = None
    error: Optional[str] = None


class FounderJobListResponse(BaseModel):
    jobs: list[FounderJobSummary]


def _cancellable_statuses() -> frozenset[str]:
    from agent_team_studio.user_agent_founder.shared import job_store

    return frozenset({job_store.JOB_STATUS_PENDING, job_store.JOB_STATUS_RUNNING})


@app.get("/jobs", response_model=FounderJobListResponse)
def list_jobs(running_only: bool = False) -> FounderJobListResponse:
    """List founder workflow jobs from the centralized job service."""
    from agent_team_studio.user_agent_founder.shared import job_store

    statuses = (
        [job_store.JOB_STATUS_RUNNING, job_store.JOB_STATUS_PENDING] if running_only else None
    )
    raw = job_store.list_jobs(statuses=statuses)
    jobs = []
    for j in raw:
        data = j.get("data", j)
        jobs.append(
            FounderJobSummary(
                job_id=j.get("job_id", ""),
                status=j.get("status", data.get("status", "unknown")),
                label=data.get("label", "Testing Personas workflow"),
                current_phase=data.get("current_phase"),
                created_at=j.get("created_at", data.get("created_at")),
                error=data.get("error"),
            )
        )
    return FounderJobListResponse(jobs=jobs)


@app.post("/job/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, str]:
    """Cancel a running founder workflow job.

    Preconditions:
        - ``job_id`` refers to a job in a cancellable status (see
          ``_cancellable_statuses()``); a missing or non-cancellable job raises
          ``HTTPException`` (404/400) before any write.
    Postconditions:
        - The central job is marked CANCELLED and the founder run row "failed"
          (both carrying the "Cancelled by user" error) — this pair of writes
          always happens and is what the response reflects.
        - Best-effort, additive: when Temporal is enabled, also signals the
          in-flight ``UserAgentFounderWorkflow`` to stop at its next
          cancellation check. This signal is never required for the cancel to
          succeed — any failure to deliver it (worker down, RPC timeout) is
          caught and logged at DEBUG, never raised or reflected in the
          response, since the two writes above are already the durable record
          of the cancellation.
    """
    from agent_team_studio.user_agent_founder.shared import job_store

    try:
        job_store.validate_job_for_action(
            job_store.get_job(job_id), job_id, _cancellable_statuses(), "cancelled"
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    job_store.update_job(job_id, status=job_store.JOB_STATUS_CANCELLED, error="Cancelled by user")
    store = get_founder_store()
    # The centralized job service has a dedicated CANCELLED status; the founder run
    # store does not — its status vocabulary only has a terminal-non-success value,
    # "failed" (the orchestrator likewise maps a target-team cancellation to
    # "failed" in _run_phase). So a user cancel is CANCELLED on the job and "failed"
    # on the run row by design; both carry the "Cancelled by user" error for the
    # audit trail. (Behavior predates the Temporal work; documented here per review.)
    store.update_run(job_id, status="failed", error="Cancelled by user")

    # Best-effort: signal the Temporal workflow so its poll loops stop at the next
    # tick instead of running to completion after the store already says cancelled.
    # Thread mode has no workflow to signal — its poll loop observes the cancelled
    # job status directly — so this is Temporal-only and never fatal to the cancel.
    try:
        from shared.temporal import is_temporal_enabled

        if is_temporal_enabled():
            from agent_team_studio.user_agent_founder.temporal.start_workflow import (
                cancel_founder_workflow,
            )

            cancel_founder_workflow(job_id)
    except Exception:
        logger.debug("Temporal cancel signal failed for %s (non-fatal)", job_id, exc_info=True)

    return {"status": job_store.JOB_STATUS_CANCELLED, "job_id": job_id}


@app.post("/job/{job_id}/resume", response_model=StartRunResponse)
def resume_job(job_id: str) -> StartRunResponse:
    """Resume a failed or interrupted founder workflow.

    The founder workflow has no user inputs, so "resume" re-runs the
    three phases from the beginning — the store retains any previous
    decisions and chat history for audit.
    """
    from agent_team_studio.user_agent_founder.shared import job_store

    try:
        job_store.validate_job_for_action(
            job_store.get_job(job_id), job_id, job_store.RESUMABLE_STATUSES, "resumed"
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    job_store.update_job(
        job_id, status=job_store.JOB_STATUS_RUNNING, error=None, current_phase="resuming"
    )
    store = get_founder_store()
    store.update_run(job_id, status="pending", error=None)

    try:
        mode = _dispatch_founder_run(job_id)
    except Exception as exc:
        logger.exception("Failed to resume founder workflow for %s", job_id)
        job_store.update_job(
            job_id,
            status=job_store.JOB_STATUS_FAILED,
            error=f"Resume dispatch failed: {exc}",
        )
        store.update_run(job_id, status="failed", error=f"Resume dispatch failed: {exc}")
        # Full exception logged above; return a generic detail (no internal text).
        raise HTTPException(status_code=500, detail="Failed to resume workflow.") from exc

    return StartRunResponse(
        job_id=job_id,
        status=job_store.JOB_STATUS_RUNNING,
        message=f"Founder workflow resumed ({mode}).",
    )


@app.post("/job/{job_id}/restart", response_model=StartRunResponse)
def restart_job(job_id: str) -> StartRunResponse:
    """Restart a completed/failed/cancelled founder workflow from scratch.

    Clears any prior error/phase on both the central job record and the
    founder store row, then re-dispatches the pipeline.
    """
    from agent_team_studio.user_agent_founder.shared import job_store

    try:
        job_store.validate_job_for_action(
            job_store.get_job(job_id), job_id, job_store.RESTARTABLE_STATUSES, "restarted"
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    job_store.reset_job(job_id)
    job_store.update_job(job_id, status=job_store.JOB_STATUS_RUNNING, current_phase="starting")
    store = get_founder_store()
    # Clear every checkpoint column so the orchestrator's resume short-circuit
    # (which keys off non-NULL spec_content / analysis_job_id / repo_path /
    # se_job_id) cannot turn a restart into a stale replay.
    store.update_run(
        job_id,
        status="pending",
        error=None,
        spec_content=None,
        analysis_job_id=None,
        repo_path=None,
        se_job_id=None,
    )

    try:
        mode = _dispatch_founder_run(job_id)
    except Exception as exc:
        logger.exception("Failed to restart founder workflow for %s", job_id)
        job_store.update_job(
            job_id,
            status=job_store.JOB_STATUS_FAILED,
            error=f"Restart dispatch failed: {exc}",
        )
        store.update_run(job_id, status="failed", error=f"Restart dispatch failed: {exc}")
        # Full exception logged above; return a generic detail (no internal text).
        raise HTTPException(status_code=500, detail="Failed to restart workflow.") from exc

    return StartRunResponse(
        job_id=job_id,
        status=job_store.JOB_STATUS_RUNNING,
        message=f"Founder workflow restarted ({mode}).",
    )


@app.delete("/job/{job_id}")
def delete_job(job_id: str) -> dict[str, str]:
    """Delete a founder workflow job from both the job service and store."""
    from agent_team_studio.user_agent_founder.shared import job_store

    if job_store.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    job_store.delete_job(job_id)
    store = get_founder_store()
    try:
        store.delete_run(job_id)
    except Exception:
        logger.debug("Founder store delete_run failed for %s (non-fatal)", job_id, exc_info=True)
    return {"deleted": "true", "job_id": job_id}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
