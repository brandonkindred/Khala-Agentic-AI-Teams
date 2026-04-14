# Branding Team — System Design Documentation

This folder is the architectural and design reference for the branding team
(`backend/agents/branding_team/`). It documents the team as it exists in code,
not as it was originally scoped. The team has evolved from a flat 6-agent
pipeline into a **5-phase enterprise branding framework** with phase gates,
multi-brand agency persistence, and three concurrent API styles.

The top-level `backend/agents/branding_team/README.md` remains the
user-facing operational reference (endpoints, curl examples, env vars). These
documents focus on **why** and **how** — the design decisions, data flows,
and internal structure.

## Documents in this folder

| Document | Purpose |
|---|---|
| [`architecture.md`](./architecture.md) | Static architecture: layered component diagram, architectural principles, key design decisions. |
| [`system_design.md`](./system_design.md) | Detailed system design: module layout, domain model, state machines, API surface, persistence, LLM integration, runtime modes, configuration. |
| [`use_cases.md`](./use_cases.md) | Actors and numbered use cases with triggers, preconditions, main flows, and entry-point endpoints. |
| [`flow_charts.md`](./flow_charts.md) | Operational flow and sequence diagrams for every runtime path (5-phase pipeline, sync run, session Q&A, chat, phase-gated approvals, agency lifecycle, adapters, Temporal). |

## Source files referenced across these documents

| Area | Path |
|---|---|
| Orchestrator | `backend/agents/branding_team/orchestrator.py` |
| Specialist agents | `backend/agents/branding_team/agents.py` |
| Domain models | `backend/agents/branding_team/models.py` |
| FastAPI app | `backend/agents/branding_team/api/main.py` |
| Client/brand store | `backend/agents/branding_team/store.py` |
| DB path resolver | `backend/agents/branding_team/db.py` |
| Assistant agent | `backend/agents/branding_team/assistant/agent.py` |
| Assistant prompts | `backend/agents/branding_team/assistant/prompts.py` |
| Conversation store | `backend/agents/branding_team/assistant/store.py` |
| Market research adapter | `backend/agents/branding_team/adapters/market_research.py` |
| Design assets adapter | `backend/agents/branding_team/adapters/design_assets.py` |
| Postgres schema | `backend/agents/branding_team/postgres/__init__.py` |
| Temporal wrapper | `backend/agents/branding_team/temporal/__init__.py` |
| Team mount config | `backend/unified_api/config.py` |

## Conventions used in this documentation

- **Mermaid only** for diagrams — matches the existing `README.md` style.
- **Line-referenced citations** (e.g. `orchestrator.py:147`) where a claim
  describes specific code behavior.
- **Neutral technical tone**, no emojis.
- All file paths are relative to the repository root.
