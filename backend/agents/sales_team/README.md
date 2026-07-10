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

The synchronous single-stage endpoints and `/sales/prospect/deep-research` run
in-request and are unaffected by the mode.

### Temporal tuning knobs

| Variable | Default | Purpose |
|---|---|---|
| `SALES_TEMPORAL_MAX_CONCURRENT_ACTIVITIES` | `8` | Sales worker's concurrent-activity ceiling (bounds per-prospect fan-out throughput). |
| `SALES_TEMPORAL_HEARTBEAT_INTERVAL_S` | `30` | Heartbeat cadence for long LLM activities (clamped to `[1, 60]`s, always under the 180s heartbeat timeout). |

Full semantics: [`docs/ENV_VARS.md`](../../../docs/ENV_VARS.md) → "Temporal, Security, and Logging".

## Khala platform

This package is part of the [Khala](../../../README.md) monorepo (Unified API, Angular UI, and full team index).
