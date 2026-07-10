# AI Sales Team

B2B sales pod: prospecting, outreach, qualification, nurturing, discovery, proposals, and closing. Exposed via the Unified API at `/api/sales`.

## Execution modes

The durable job pipeline (`POST /sales/pipeline/run`) runs in one of two modes:

- **Thread mode** (default): `SalesPodOrchestrator.run` drives the 7 stages in
  process, fanning out per-prospect work over a bounded thread pool.
- **Temporal mode** (`TEMPORAL_ADDRESS` set): `SalesWorkflow` orchestrates the
  pipeline as fine-grained activities — one per prospect per stage — so every
  specialist agent call is durable and individually retryable. Gating, routing,
  and the per-prospect stage logic are single-sourced (`routing.py`,
  `orchestrator.build_run_context`/`<stage>_one`) so the two modes cannot drift.

The **deep-research** prospecting pipeline (company → decision-maker → dossier)
has both a synchronous endpoint (`POST /sales/prospect/deep-research`, runs
in-request) and a **durable job** counterpart:

- `POST /sales/prospect/deep-research/run` → returns a `job_id`; poll
  `GET /sales/prospect/deep-research/status/{job_id}` for the ranked result.
- In Temporal mode `DeepResearchWorkflow` fans out one activity per company
  (decision-maker mapping) and one per prospect (dossier building); in thread
  mode `run_deep_research_job` runs the same `deep_research_only` body in a
  daemon thread. Both share the per-item cores (`map_company_one` /
  `build_dossier_one`) and the assembly/persist step
  (`assemble_and_persist_deep_research`) with the synchronous path.

Use the job endpoint for large `target_prospects`, where the pipeline runs
longer than a request should block and a worker restart must not lose the run.
The other synchronous single-stage endpoints are unaffected by the mode.

### Temporal tuning knobs

| Variable | Default | Purpose |
|---|---|---|
| `SALES_TEMPORAL_MAX_CONCURRENT_ACTIVITIES` | `8` | Sales worker's concurrent-activity ceiling (bounds per-prospect fan-out throughput). |
| `SALES_TEMPORAL_HEARTBEAT_INTERVAL_S` | `30` | Heartbeat cadence for long LLM activities (clamped to `[1, 60]`s, always under the 180s heartbeat timeout). |

Full semantics: [`docs/ENV_VARS.md`](../../../docs/ENV_VARS.md) → "Temporal, Security, and Logging".

## Khala platform

This package is part of the [Khala](../../../README.md) monorepo (Unified API, Angular UI, and full team index).
