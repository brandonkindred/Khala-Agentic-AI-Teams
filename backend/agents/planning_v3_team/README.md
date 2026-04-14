# Planning V3 Team

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
| **Document production** | Write context doc and spec; call PRA (and optionally Planning V2); persist artifacts. |
| **Sub-agent provisioning** | Optional: when capability gap identified, draft agent spec, call AI Systems, store blueprint. |

## Adapters

Planning V3 calls other teams via HTTP:

| Adapter | Purpose |
|---------|---------|
| **product_analysis** | Product Requirements Analysis (SE API): run, poll status, submit answers; get validated spec and PRD. |
| **planning_v2** | Planning V2 (SE API): run, poll, get result; attach plan artifacts to handoff. |
| **market_research** | Market Research API: user/customer discovery; map response into context/evidence. |
| **ai_systems** | AI Systems Team: build a new agent from a spec; poll status; store blueprint. |

## Environment variables

- **`UNIFIED_API_BASE_URL`** – Base URL for all adapters (e.g. `http://localhost:8080` when using the unified API).
- **`PLANNING_V3_SOFTWARE_ENGINEERING_URL`** – Override for SE API (product-analysis, planning-v2).
- **`PLANNING_V3_MARKET_RESEARCH_URL`** – Override for Market Research API.
- **`PLANNING_V3_AI_SYSTEMS_URL`** – Override for AI Systems build/status API.
- **`AGENT_CACHE`** – Cache directory for job store (default `.agent_cache`).

## API (mounted at `/api/planning-v3`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/run` | Start Planning V3; body: `PlanningV3RunRequest`; returns `job_id`. |
| GET | `/status/{job_id}` | Job status, phase, progress, pending questions. |
| GET | `/result/{job_id}` | Handoff package and artifact paths when completed. |
| GET | `/jobs` | List running/pending jobs. |
| POST | `/{job_id}/answers` | Submit answers to open questions (when waiting_for_answers). |
| GET | `/health` | Health check. |

## How downstream teams use the handoff

- **Dev / UI / UX**: Consume the handoff package: `client_context_document_path`, `validated_spec_path`, `prd_path`, and optional `planning_v2_artifact_paths` (e.g. `architecture.md`, `task_breakdown.md`). All paths are under the same `repo_path` (e.g. `plan/client_context.md`, `plan/product_analysis/validated_spec.md`, `plan/product_analysis/product_requirements_document.md`).
- **Software Engineering Team**: Can run with `repo_path` pointing at the same folder; it will find `initial_spec.md` or the validated spec under `plan/` and use the PRD as context.
- **Optional sub-agent**: If `sub_agent_blueprint` is present in the handoff, it describes an agent built by the AI Systems Team for a capability gap; use or register it as needed.

## Directory structure

```
planning_v3_team/
├── __init__.py
├── README.md
├── models.py           # Request/response, Phase, ClientContext, HandoffPackage, OpenQuestion
├── orchestrator.py     # run_workflow: phase order, adapters, LLM
├── adapters/
│   ├── __init__.py
│   ├── product_analysis.py
│   ├── planning_v2.py
│   ├── market_research.py
│   └── ai_systems.py
├── phases/
│   ├── __init__.py
│   ├── intake.py
│   ├── discovery.py
│   ├── requirements.py
│   ├── synthesis.py
│   ├── document_production.py
│   └── sub_agent_provisioning.py
├── shared/
│   ├── __init__.py
│   └── job_store.py
└── api/
    ├── __init__.py
    └── main.py
```

## Khala platform

This package is part of the [Khala](../../../README.md) monorepo (Unified API, Angular UI, and full team index).
