# Market Research & Concept Viability Team

This team is designed as a **human-AI collaborative workflow** for discovering what users need and whether a product concept is viable.

## Recommended structure

Use a **single orchestrator with selectable topology**:

- `unified` (default): one cohesive team pass when speed is the priority.
- `split`: explicit discovery and viability phases when rigor and handoff checkpoints matter.

This gives you both options without duplicating implementation.

## What this team does

1. Ingests interview transcripts (inline or via `transcript_folder_path`).
2. Extracts UX insights (jobs, pains, desired outcomes).
3. Synthesizes user psychology/market signals.
4. Produces a viability recommendation with confidence and rationale.
5. Generates practical research scripts for the next sprint.
6. Waits for **human approval** before marking work ready for execution.

## API

Start:

```bash
uvicorn market_research_team.api.main:app --reload --host 0.0.0.0 --port 8010
```

Run (async):

```http
POST /market-research/run        # submit -> { job_id, status }
GET  /market-research/status/{job_id}
GET  /market-research/jobs
POST /market-research/jobs/{job_id}/cancel
DELETE /market-research/jobs/{job_id}
```

Example submit payload:

```json
{
  "product_concept": "AI-powered interview synthesis workspace",
  "target_users": "product managers at B2B SaaS companies",
  "business_goal": "reduce time to validated roadmap decisions",
  "topology": "split",
  "transcript_folder_path": "./sample_transcripts",
  "human_approved": false,
  "human_feedback": "Need evidence about willingness to pay before greenlighting MVP"
}
```

The response is `{ "job_id": "...", "status": "pending" }`. Poll
`GET /market-research/status/{job_id}` until `status` is `completed`;
the `TeamOutput` is then in the `result` field.

## Execution model

The pipeline runs the same DAG in two modes from one shared per-stage seam
(`MarketResearchOrchestrator`'s `ingest` / `ux_one` / `psychology` /
`consistency` / `viability` / `scripts` / `assemble` methods):

- **Thread mode** (default; `TEMPORAL_ADDRESS` unset): `orchestrator.run` fans
  the UX stage out one call per transcript with a bounded thread pool.
- **Temporal mode** (`TEMPORAL_ADDRESS` set): `MarketResearchWorkflow`
  orchestrates the DAG as a graph of durable, individually-retryable activities
  (`temporal/activities.py`) — a single-shot prepare/ingest/finalize plus one
  `ux_one` activity per transcript and the psychology/consistency/viability/
  scripts specialist stages, each visible in the Temporal UI. Job-store status
  is owned by prepare (RUNNING), finalize (COMPLETED), and mark-failed (FAILED).
  To keep (potentially large) transcript bodies out of Temporal workflow
  history, `ingest` persists them to a per-job transcript store on the shared
  `AGENT_CACHE` volume (`shared/transcript_store.py`) and passes only
  lightweight refs through the workflow; each `ux_one` loads its own transcript
  from the store, which finalize/mark-failed clear at the end of the run.

Both modes share the specialist agents in `agents.py`, so they produce the same
`TeamOutput`.

## Khala platform

This package is part of the [Khala](../../../README.md) monorepo (Unified API, Angular UI, and full team index).
