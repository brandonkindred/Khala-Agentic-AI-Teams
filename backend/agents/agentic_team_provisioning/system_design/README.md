# Agentic Team Provisioning — System Design

This folder captures the architectural and design decisions of the **Agentic Team Provisioning** team in diagram-first form. It complements the existing normative contract ([`../AGENTIC_TEAM_ARCHITECTURE.md`](../AGENTIC_TEAM_ARCHITECTURE.md)) and the legacy draw.io exports in [`../designs/`](../designs/) by:

- filling in the orchestrator internals and external integrations the legacy PNGs omit,
- enumerating concrete use cases behind each API Layer category, and
- animating the static structures with sequence, flow, and state diagrams.

All diagrams use **Mermaid** (renderable directly on GitHub) and reuse the vocabulary from the legacy PNGs verbatim — `API Layer`, `Orchestrator Agent`, `Agents` / `Agents pool` / `Roster`, `Processes` / `Processes pool`, `Job Tracking`, `Question Tracking`, `Actor`, `UI`, `Agentic Team`, `File System`, `Database` — so readers can jump between PNG and Mermaid without relearning terms.

## Documents

| Document | What it covers | Extends |
|---|---|---|
| [`architecture.md`](architecture.md) | Layered / container architecture of the team. Expands the Orchestrator Agent into its concrete components, adds the 2 API categories (Assets, Form Information) missing from the legacy internal diagram, adds Testing / Pipeline endpoints, and wires in the external dependencies (LLM service, Agent Provisioning team, Temporal, shared_postgres). | [`../designs/Agentic-team-architecture.png`](../designs/Agentic-team-architecture.png) |
| [`system_design.md`](system_design.md) | Module dependency graph, ER diagram for the persistence schema (shared SQLite + per-team SQLite + optional Postgres JSONB), Pydantic model catalogue, and runtime-mode decision tree. | Goes beneath both legacy PNGs with a data/module view they don't show. |
| [`use_cases.md`](use_cases.md) | Actor → UI → API Layer use-case map. Enumerates the concrete use cases that flow through each of the 5 API categories from the API interactions PNG, plus Testing Chat and Pipeline Runs. | [`../designs/AgenticTeamApiInteractionsArchitecture.png`](../designs/AgenticTeamApiInteractionsArchitecture.png) |
| [`flow_charts.md`](flow_charts.md) | Sequence diagrams (conversational team design, agent env provisioning bridge, asset upload, form record write), flow charts (roster validation, pipeline run), and state diagrams (`PipelineRunStatus`, `TeamMode`). | Animates the static boxes from both legacy PNGs. |

## Accuracy notes — read these before designing a test pipeline

These docs describe the code **as implemented**, not what the `StepType` enum or `ProcessDesignerAgent` system prompt suggest is possible. Two gaps to be aware of:

1. **`PipelineRunner` walks a linear topological order.** `runtime/pipeline_runner.py:83` pre-computes `step_order = _topological_sort(process.steps)` once and iterates it linearly at lines 90-115. Only `WAIT` and `DECISION` have dedicated handlers; `PARALLEL_SPLIT`, `PARALLEL_JOIN`, and `SUBPROCESS` all fall through to `_handle_action_step`. Even `DECISION` does **not** alter traversal — it runs the agent, records the decision string, and advances to the next topologically-sorted step. Do not design pipeline tests that depend on fan-out, barriers, recursion, or branch selection; they will silently run as plain linear sequences. Details and the full "expected vs. actual" table are in [`flow_charts.md`](flow_charts.md#6-flowchart--pipeline-test-run-animates-uc9).

2. **`TeamMode` is advisory metadata, not a server-side gate.** `PUT /teams/{id}/mode` (`api/main.py:670-677`) records the mode, but `create_test_chat_session` (`:694`), `send_test_chat_message` (`:760`), and `start_pipeline_run` (`:858`) never read it. A team in `DEVELOPMENT` mode can still accept test chat sessions and pipeline runs. Treat `TeamMode` as a UI hint, not a security boundary. See [`use_cases.md` UC8](use_cases.md#uc8--interactive-testing-chat-with-a-rostered-agent-new) and [`flow_charts.md` §8](flow_charts.md#8-state--teammode-advisory-metadata-only).

## Related references

- [`../AGENTIC_TEAM_ARCHITECTURE.md`](../AGENTIC_TEAM_ARCHITECTURE.md) — **normative contract** for every agentic team produced by this service. Do not contradict.
- [`../README.md`](../README.md) — short service overview.
- [`../designs/Agentic-team-architecture.png`](../designs/Agentic-team-architecture.png) — legacy internal architecture (API Layer / Orchestrator Agent / Agents / Processes / Job·Question Tracking).
- [`../designs/AgenticTeamApiInteractionsArchitecture.png`](../designs/AgenticTeamApiInteractionsArchitecture.png) — legacy Actor → UI → API Layer → Agentic Team / File System / Database view.
- `backend/unified_api/config.py:194-197` — `TeamConfig` entry mounting this service at `/api/agentic-team-provisioning`.

## Diagram style conventions

All Mermaid diagrams use a consistent palette so the same concept keeps the same colour across every document:

| Role | Mermaid classDef |
|---|---|
| API Layer boxes | `fill:#e8f0fe,stroke:#1a73e8` |
| Orchestrator Agent + internals | `fill:#fff4e5,stroke:#e8710a` |
| Agents pool / Roster | `fill:#e6f4ea,stroke:#188038` |
| Processes pool | `fill:#fce8e6,stroke:#c5221f` |
| Job / Question Tracking | `fill:#f3e8fd,stroke:#8430ce` |
| External dependencies (new, not in legacy PNGs) | `fill:#f1f3f4,stroke:#5f6368,stroke-dasharray: 3 3` |

External dependency nodes always use a dashed stroke so readers can immediately see what content has been added beyond the legacy PNGs.

## Last reviewed

Against the working tree on branch `claude/document-team-provisioning-architecture-gNlHj` (2026-04-11). Revisit these diagrams whenever `api/main.py`, `assistant/agent.py`, `runtime/pipeline_runner.py`, `roster_validation.py`, `agent_env_provisioning.py`, `infrastructure.py`, `postgres/__init__.py`, or `models.py` change materially.
