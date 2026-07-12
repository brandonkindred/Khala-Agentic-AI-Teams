# Deepthought

Recursive self-organising agent that dynamically creates specialist sub-agents to answer complex questions. Unified API prefix: `/api/deepthought`.

## Execution model

`POST /deepthought/ask` runs the recursive pipeline one of two ways, both funnelling through the same reasoning logic and the same job-store status transitions, so `GET /deepthought/status/{job_id}` polling is identical:

- **Thread mode** (Temporal disabled): `DeepthoughtOrchestrator.process_message` runs on a background thread.
- **Temporal mode** (`TEMPORAL_ADDRESS` set): a durable `DeepthoughtWorkflow` drives the recursion, with **one Temporal activity per LLM boundary** — `classify_strategy`, `analyse`, `force_direct_answer`, `deliberate`, `synthesise` — plus job-store `start`/`finalize` activities. The workflow itself is deterministic: it uses `workflow.uuid4` for agent ids, `asyncio.gather` for per-level fan-out, and holds the per-run knowledge base, the agent budget, and the event log as workflow state. The pure tree-shaping rules live in `deepthought/reasoning.py` and are shared verbatim by both runtimes.

`POST /deepthought/ask/stream` (SSE) always runs in-process on a thread, regardless of Temporal.

### Temporal-mode behavioural notes

- The per-run **knowledge-base dedup** and the **50-agent budget** are preserved (as deterministic workflow state); budget is reserved in list order rather than raced across threads.
- The cross-request **`ResultCache`** (TTL/wall-clock based) is **bypassed** in Temporal mode — it cannot run deterministically inside a workflow. Thread and SSE modes still use it.
- In-flight `DeepthoughtWorkflow` histories started before the decomposition keep replaying through the legacy single-activity path via `workflow.patched("deepthought-decomposed-pipeline")`.

Temporal wiring lives in `deepthought/temporal/` (`workflows.py`, `activities.py`, `constants.py`, `phase_models.py`); the worker boots via `deepthought.temporal.worker` (Pattern A — importing the package has no side effects). See `backend/agents/shared_temporal/README.md`.

## Khala platform

This package is part of the [Khala](../../../README.md) monorepo (Unified API, Angular UI, and full team index).
