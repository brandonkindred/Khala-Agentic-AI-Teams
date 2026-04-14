# Startup Advisor — System Design Documentation

This folder is the architectural and design reference for the startup
advisor team (`backend/agents/startup_advisor/`). It documents the
team as it exists in code today: a persistent, conversational startup
advisor that runs as a **single FastAPI app with one specialist LLM
agent**, a **singleton per-deployment conversation** persisted in
Postgres, and an optional Temporal workflow wrapper around its
message handler.

The top-level `backend/agents/startup_advisor/README.md` stays the
user-facing operational reference (prefix, one-line description).
These documents focus on **why** and **how** — the design decisions,
data flow, and internal structure — so future contributors do not
have to reverse-engineer the team from source files.

## Documents in this folder

| Document | Purpose |
|---|---|
| [`architecture.md`](./architecture.md) | Static architecture: layered component diagram, architectural principles, key design decisions. |
| [`system_design.md`](./system_design.md) | Detailed system design: module layout, domain model, API surface, persistence schema, LLM integration, runtime modes, configuration. |
| [`use_cases.md`](./use_cases.md) | Actors and numbered use cases with triggers, preconditions, main/alternate flows, and entry-point endpoints. |
| [`flow_charts.md`](./flow_charts.md) | Sequence and flow diagrams for every runtime path (get-or-create, probing dialogue, artifact generation, LLM fallback, context accumulation, Temporal, lifespan). |

## Source files referenced across these documents

| Area | Path |
|---|---|
| FastAPI app / endpoints | `backend/agents/startup_advisor/api/main.py` |
| Conversational agent | `backend/agents/startup_advisor/assistant/agent.py` |
| Postgres store | `backend/agents/startup_advisor/store.py` |
| Postgres schema | `backend/agents/startup_advisor/postgres/__init__.py` |
| Temporal wrapper | `backend/agents/startup_advisor/temporal/__init__.py` |
| Store unit tests | `backend/agents/startup_advisor/tests/test_store.py` |
| Team mount config | `backend/unified_api/config.py` (lines 202-209) |
| Top-level README | `backend/agents/startup_advisor/README.md` |

## Conventions used in this documentation

- **Mermaid only** for diagrams — matches the existing team
  documentation style (branding, investment, accessibility audit).
- **Line-referenced citations** (e.g. `store.py:231`) whenever a
  claim describes specific code behavior.
- **Neutral technical tone**, no emojis.
- All file paths are relative to the repository root.
- The team has **no explicit orchestrator module** — the FastAPI
  route handler itself coordinates store + agent. These docs reflect
  that faithfully rather than inventing a "team lead" layer.
- The **singleton conversation** design (one row in
  `startup_advisor_conversations` per deployment) is described as a
  deliberate choice; see `store.py:231-247` and the cross-reference
  in `architecture.md`.
- **Temporal** flows are marked as optional, gated on
  `is_temporal_enabled()` (`temporal/__init__.py:38-41`).
