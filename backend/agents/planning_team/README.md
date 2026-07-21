# Planning Team

Client-facing **product owner / pre-sales discovery** team: first leg of the software development process. Understands client and user context, problem and requirements (including RPO/RTO and agency expectations), produces PRD and related documents, and can call other agents or set up sub-agents via the AI Systems Team.

## Purpose

- **Client & user context**: Who is the client, who are their customers, business context, success criteria.
- **Problem & opportunity**: What problem we are solving, for whom, why now; scope boundaries.
- **Requirements & constraints**: RPO, RTO, SLAs, compliance, security, tech constraints (what a client PO would document).
- **Evidence synthesis**: Optional Market Research (user/customer discovery); consolidate into one context.
- **Document production**: Client context document, validated spec, PRD, handoff package for dev/UI/UX.
- **Sub-agents**: When a capability is missing, call the AI Systems Team to build a new agent; use or register it.

## Phases

| Phase | Description |
|-------|-------------|
| **Intake** | Client identity, initial brief/spec, existing artifacts. |
| **Discovery** | Problem statement, opportunity, personas, success criteria (LLM). |
| **Requirements** | RPO, RTO, SLAs, compliance, security, tech constraints; open questions with options. |
| **Synthesis** | Optional Market Research; merge evidence into context. |
| **Document production** | Write context doc and spec; call PRA; run the architecture step; persist artifacts. |
| **Sub-agent provisioning** | Optional: when capability gap identified, draft agent spec, call AI Systems, store blueprint. |

## Agent anatomy

Each phase's real logic lives in an anatomy-conformant persona-agent package under
`agents/<phase>/` (see [`AGENT_ANATOMY.md`](../agent_provisioning_team/AGENT_ANATOMY.md)),
and the matching `phases/<phase>.py::run_*` is a **thin adapter** that maps the workflow
`context` dict to the agent's typed Input and maps the typed Output back to the
`(context_update, artifacts)` tuple. The `orchestrator.run_workflow` coordinator (§2) and
the per-phase Temporal activities call the same `run_*` seam, so both drivers stay in sync.

Each `agents/<phase>/` package holds:

- **`models.py`** — typed `*Input`/`*Output` boundary contracts (§1). Shared domain models
  (`ClientContext`, `OpenQuestion`, `HandoffPackage`) are reused from `models.py`, not redefined.
- **`agent.py`** — a stateless agent class with a `run(...)` method. Injected callables
  (`llm`, `run_pra`/`wait_pra`, `answer_callback`, `run_architecture_fn`,
  `start_build_fn`/`wait_build_fn`) are the agent's declared **tools** (§3), passed as method
  params. Guardrails are enforced in code, not prompt text (§6).
- **`prompts.py`** — *discovery* and *requirements* only. The LLM runtime
  (`LLMClient.complete_text`) accepts a single prompt string with **no** `system_prompt`
  parameter, so `AGENT_ANATOMY.md` §5's System/User split lives in code: a `SYSTEM_PROMPT`
  constant + `build_user_prompt(...)`, re-joined by `build_prompt(...)` into the exact string
  the runtime consumes. `tests/test_prompts.py` pins that join byte-identical to the
  pre-split literal.

Per-phase Input→Agent→Output diagrams: [`system_design/agent_anatomy.md`](system_design/agent_anatomy.md).

| Agent package | LLM? | Declared tools (§3) |
|---------------|------|---------------------|
| `agents/intake` | no | — |
| `agents/discovery` | yes | `llm` |
| `agents/requirements` | yes | `llm` |
| `agents/synthesis` | no | — |
| `agents/document_production` | no* | `run_pra`, `wait_pra`, `answer_callback`, `run_architecture_fn` |
| `agents/sub_agent_provisioning` | no | `start_build_fn`, `wait_build_fn` |

\* Document production does not call the LLM directly; the architecture overview is compacted
via `compact_text` inside the phase-module helper it reuses.

## Adapters

The Planning team calls other teams via HTTP:

| Adapter | Purpose |
|---------|---------|
| **product_analysis** | Product Requirements Analysis (SE API): run, poll status, submit answers; get validated spec and PRD. |
| **market_research** | Market Research API: user/customer discovery; map response into context/evidence. |
| **ai_systems** | AI Systems Team: build a new agent from a spec; poll status; store blueprint. |

Each adapter resolves its target service's base URL and builds request URLs
through a shared `BaseAdapter` (`adapters/_base.py`) rather than reimplementing
that resolution itself; HTTP calls and polling still go through
`shared_http.job_polling` directly in each adapter module.

## Environment variables

- **`UNIFIED_API_BASE_URL`** – Base URL for all adapters (e.g. `http://localhost:8080` when using the unified API).
- **`PLANNING_SOFTWARE_ENGINEERING_URL`** – Override for SE API (product-analysis).
- **`PLANNING_MARKET_RESEARCH_URL`** – Override for Market Research API.
- **`PLANNING_AI_SYSTEMS_URL`** – Override for AI Systems build/status API.
- **`AGENT_CACHE`** – Cache directory for job store (default `.agent_cache`).

## API (mounted at `/api/planning`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/run` | Start the Planning workflow; body `PlanningRunRequest`, returns `job_id`. See the note below. |
| GET | `/status/{job_id}` | Job status, phase, progress, pending questions. |
| GET | `/result/{job_id}` | Handoff package and artifact paths when completed. |
| GET | `/jobs` | List running/pending jobs. |
| POST | `/{job_id}/answers` | Submit answers to open questions (when waiting_for_answers). |
| GET | `/health` | Health check. |

**`/run` body notes:** `repo_path` is an **optional** label for the plan's output
folder (never read as source). Every run is confined to a fresh directory under
`AGENT_CACHE/planning/` — an empty value, a git URL, or a filesystem path is
reduced to a single sanitized segment, so a supplied path can never write outside
the cache root. At least one of `initial_brief`/`spec_content` is required.

## Execution model (thread vs Temporal)

`POST /run` records a job and dispatches the pipeline one of two ways, deciding on
`TEMPORAL_ADDRESS`:

- **Thread mode** (default, `TEMPORAL_ADDRESS` unset): the orchestrator
  (`orchestrator.run_workflow`) runs all phases in a background thread.
- **Temporal mode** (`TEMPORAL_ADDRESS` set): `PlanningWorkflow` (`temporal/`) drives
  the **same phase sequence**, but **each phase is its own `@activity.defn`** so
  Temporal records, times out, retries and (for the long PRA / AI-Systems polls)
  heartbeats every phase independently — not one opaque black-box activity.

The two paths share the phase functions in `phases/`, so they produce the same
handoff (a parity test pins this). The mutable phase `context` crosses each
activity boundary as a JSON-native dict — the activity wrappers re-hydrate models
inside the phase functions and normalize `ClientContext`/`HandoffPackage` back to
dicts on the way out. Progress and terminal status are written to the durable job
store, so a completed run survives a worker restart and the API keeps polling
`GET /status/{job_id}`.

| Phase activity | Timeout | Retry |
|----------------|---------|-------|
| `intake` / `synthesis` / `finalize` | 5 min | up to 3 (deterministic, safe) |
| `discovery` / `requirements` | 1 h | up to 3 (pure LLM extraction — writes/submits nothing, safe to retry) |
| `market_research` | 1 h | 1 (submits an external research request) |
| `document_production` / `sub_agent_provisioning` | 2 h (+ 5 min heartbeat) | 1 (writes files / submits external jobs) |

The worker is registered via `shared.temporal.start_team_worker` (Pattern A:
`temporal/__init__.py` exports `WORKFLOWS`/`ACTIVITIES`) and started per uvicorn
worker by the `team_service` entrypoint (`TEAM_TEMPORAL_WORKER_MODULE` /
`TEAM_TEMPORAL_WORKER_FUNC`), with the API lifespan as a standalone-dev backstop.
The task queue is `planning` (override with `TEMPORAL_TASK_QUEUE_PLANNING`); large
handoffs can enable the shared gzip payload codec via `TEMPORAL_PAYLOAD_COMPRESSION`.

## How downstream teams use the handoff

- **Dev / UI / UX**: Consume the handoff package: `client_context_document_path`, `validated_spec_path`, `prd_path`, and `architecture_overview`. All paths are under the same `repo_path` (e.g. `plan/client_context.md`, `plan/product_analysis/validated_spec.md`, `plan/product_analysis/product_requirements_document.md`).
- **Software Engineering Team**: Can run with `repo_path` pointing at the same folder; it will find `initial_spec.md` or the validated spec under `plan/` and use the PRD as context.
- **Optional sub-agent**: If `sub_agent_blueprint` is present in the handoff, it describes an agent built by the AI Systems Team for a capability gap; use or register it as needed.

## Directory structure

```
planning_team/
├── __init__.py
├── README.md
├── models.py           # Request/response, Phase, ClientContext, HandoffPackage, OpenQuestion
├── orchestrator.py     # run_workflow: phase order, adapters, LLM
├── agents/             # Anatomy-conformant persona agents (typed Input/Output, prompts)
│   ├── __init__.py
│   ├── intake/                {__init__,agent,models}.py
│   ├── discovery/             {__init__,agent,prompts,models}.py
│   ├── requirements/          {__init__,agent,prompts,models}.py
│   ├── synthesis/             {__init__,agent,models}.py
│   ├── document_production/   {__init__,agent,models}.py
│   └── sub_agent_provisioning/{__init__,agent,models}.py
├── adapters/
│   ├── __init__.py
│   ├── _base.py         # BaseAdapter: shared base-URL resolution + URL building
│   ├── product_analysis.py
│   ├── market_research.py
│   └── ai_systems.py
├── phases/             # Thin adapters over agents/ (stable run_* seam for orchestrator/Temporal)
│   ├── __init__.py
│   ├── intake.py
│   ├── discovery.py
│   ├── requirements.py
│   ├── synthesis.py
│   ├── document_production.py  # + pinned leaf helpers (_compact_architecture_overview, writers)
│   └── sub_agent_provisioning.py
├── shared/
│   ├── __init__.py
│   └── job_store.py
├── temporal/           # Durable execution: PlanningWorkflow + one activity per phase
│   ├── __init__.py     # WORKFLOWS / ACTIVITIES (Pattern A export contract)
│   ├── activities.py   # @activity.defn per phase (intake … finalize)
│   ├── workflows.py    # PlanningWorkflow: phase state machine
│   ├── worker.py       # start_planning_temporal_worker_thread (shared.temporal)
│   ├── start_workflow.py  # sync → workflow dispatch bridge
│   ├── client.py
│   └── constants.py    # TASK_QUEUE, WORKFLOW_ID_PREFIX
└── api/
    ├── __init__.py
    └── main.py
```

## Khala platform

This package is part of the [Khala](../../../README.md) monorepo (Unified API, Angular UI, and full team index).
