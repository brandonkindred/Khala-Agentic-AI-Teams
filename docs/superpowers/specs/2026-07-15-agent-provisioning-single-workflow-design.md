# Agent Provisioning: Single Temporal Workflow (No Legacy)

**Status:** Approved 2026-07-15  
**Date:** 2026-07-15  
**Type:** Breaking Temporal cutover for Agent Provisioning

## Hard rule (non-negotiable)

**No backward compatibility. No drain period. No dual registration.**

Implementation must not:

- Keep V1 or V2 workflow types registered “so in-flight runs can finish”
- Add aliases, shims, adapters, or rename wrappers that preserve old Temporal type names
- Leave `PROVISION_THREAD_FALLBACK`, thread-pool provision runners, or any in-process provision/deprovision HTTP path
- Document or test a “legacy / drain-only / temporary dual-path” mode

On deploy, old workflow executions of former type names are abandoned. Callers that need work done again use `/resume` or `/restart` against the new single workflow — that is job-store resume, not Temporal history drain.

## Problem

Agent Provisioning currently registers two Temporal workflows:

- `AgentProvisioningWorkflow` (v1) — single activity wrapping the in-process orchestrator (exists for drain/compat today; **must be deleted**, not kept through a deploy window).
- `AgentProvisioningWorkflowV2` — per-phase activities with parallel per-tool fan-out, resume via `skip_phases` / `prior_results`, and compensation on tool failure (behavior to keep; **type name and class suffix must go**).

The API already starts only V2. V1 exists solely for drain/compat. A parallel escape hatch (`PROVISION_THREAD_FALLBACK` / thread-pool `_run_provisioning_background`) still lets provision, resume, restart, and deprovision run in-process when Temporal is off or the flag is set.

That dual surface is accidental complexity: two workflow types, `_v2`-suffixed Python activity symbols, and a dispatch matrix that can silently downgrade off Temporal.

## Goals

1. Exactly one provisioning workflow: `AgentProvisioningWorkflow` (today’s V2 behavior and Temporal type name).
2. Drop all `_v2` suffixes from Python activity symbols; keep existing non-versioned Temporal activity names.
3. Delete V1 workflow, `run_provisioning_activity`, and every provision/deprovision thread fallback **in the same change** — not retained for a later PR.
4. Require Temporal for provision, resume, restart, and deprovision HTTP entrypoints — no silent downgrade.
5. Update tests and team docs so nothing describes “legacy,” “drain-only,” or “V2” as a second path.

## Non-goals

- Changing phase/orchestrator business logic (setup, credentials, tools, audit, docs, deliver, compensate).
- Merging or renaming sandbox Temporal workflows (`SandboxAcquireWorkflow`, `SandboxTeardownWorkflow`, `SandboxReaperWorkflow`).
- Any compatibility layer for old Temporal type names or in-process HTTP provision.
- Staged rollout that keeps both workflows registered for any duration.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Surviving workflow | Promote V2 body to `AgentProvisioningWorkflow` with `@workflow.defn(name="AgentProvisioningWorkflow")` | Clean long-term name; matches user choice A |
| Activity symbols | Rename `*_activity_v2` → `*_activity` | No legacy label in code |
| Temporal activity names | Keep `agent_provisioning_setup`, etc. | Already non-versioned; only delete `run_agent_provisioning` |
| Thread path | Remove entirely for provision + deprovision HTTP | User choice B + approach 1 |
| Missing Temporal | HTTP **503** with clear detail | Fail loud; no fallback |
| Deprovision | Same Temporal-only rule as provision | Avoid a leftover escape hatch |
| Compatibility / drain | **Forbidden** | No dual registration, aliases, shims, or “finish in-flight first” worker |

## Architecture

```
POST /provision | /resume | /restart
  └─ create/reset job (unchanged)
  └─ start_provisioning_workflow(...)   # required
       └─ AgentProvisioningWorkflow
            ├─ setup_activity
            ├─ credentials_activity
            ├─ provision_tool_activity × N (parallel)
            ├─ compensate_activity (on tool failure)
            ├─ audit_activity
            ├─ documentation_activity
            └─ deliver_activity

DELETE /environments/{agent_id}
  └─ run_deprovision_workflow(...)      # required
       └─ AgentDeprovisioningWorkflow
            └─ deprovision_activity
```

Sandbox workflows remain on `SANDBOX_TASK_QUEUE` and are unchanged by this design.

## Components

### Workflow (`temporal/workflows.py`)

- Single class `AgentProvisioningWorkflow` with the current V2 `run` signature:
  `job_id`, `agent_id`, `manifest_path`, `skip_phases`, `prior_results`.
- Delete the V1 class entirely.
- `AgentDeprovisioningWorkflow` stays; docstring references the single provisioning workflow (no “V2”).

### Activities (`temporal/activities.py`)

| Before | After |
|---|---|
| `run_provisioning_activity` | **deleted** |
| `setup_activity_v2` | `setup_activity` |
| `credentials_activity_v2` | `credentials_activity` |
| `audit_activity_v2` | `audit_activity` |
| `documentation_activity_v2` | `documentation_activity` |
| `deliver_activity_v2` | `deliver_activity` |
| `compensate_activity_v2` | `compensate_activity` |
| `provision_tool_activity` | unchanged |
| `deprovision_activity` | unchanged |

Worker registration (`temporal/__init__.py`) exports only the surviving workflow + renamed activities.

### API dispatch (`api/main.py`)

- Always start Temporal for provision / resume / restart / deprovision.
- Delete `_provision_thread_fallback`, dual-path `_temporal_starter` / `_deprovision_starter` (replace with helpers that raise or return 503 when Temporal is unavailable).
- Delete `_run_provisioning_background` as an HTTP submit path (and the V1 activity caller).
- Remove `PROVISION_THREAD_FALLBACK` / `provision_thread_fallback_enabled` and all callers. Sandbox dispatch (`sandbox_temporal_enabled`) must gate only on `is_temporal_enabled()` — never on a thread-fallback flag.
- Tear down the provision ThreadPoolExecutor / queue-depth 429 path used to run `_run_provisioning_background`. Retain only lifespan/cancel bookkeeping that still has a non-provision purpose; do not keep an alternate provision runner.

### Client / start (`temporal/client.py`, `temporal/start_workflow.py`)

- Start `AgentProvisioningWorkflow.run` (not V2).
- Drop fallback predicates from the client module.

## Error handling

| Condition | Behavior |
|---|---|
| Temporal disabled / client or loop missing / start cannot run durable work | HTTP 503 — Temporal required; no in-process fallback |
| Tool provisioning partial failure | Existing: compensate succeeded tools, then fail the workflow |
| In-flight jobs of old type names after deploy | Fail / abandoned — no drain worker |

## Testing

- Unit: workflow happy path, compensate, resume skip, non-dict / dict tool failures — against `AgentProvisioningWorkflow` and renamed activity symbols.
- Unit: `start_provisioning_workflow` args target the renamed workflow.
- API: Temporal enabled → starter called; Temporal disabled → 503; delete all thread-fallback / “uses thread when no temporal” assertions that expect success via in-process run.
- Registry: `WORKFLOWS` contains exactly one provisioning workflow class (+ deprovision); V1 activity absent from `ACTIVITIES`.
- Orchestrator/phase tests unchanged unless they referenced deleted helpers.
- Docs: README Temporal table drops “legacy/drain-only” and “V2” naming.

## Migration / deploy implications

Hard cutover in one change set — **not** a drain window:

1. The new worker registers only the single `AgentProvisioningWorkflow` (+ deprovision + sandbox as today).
2. Open executions typed as old `AgentProvisioningWorkflow` (single-activity) or `AgentProvisioningWorkflowV2` stop progressing; there is no drain worker and no dual registration.
3. Re-run unfinished jobs via `/resume` or `/restart` (job-store prior phases → new workflow). That is not Temporal history compatibility.
4. Local/dev without Temporal cannot provision or deprovision via the API — Temporal must be up.

## Success criteria

- Grep shows no `AgentProvisioningWorkflowV2`, `run_provisioning_activity`, `*_activity_v2`, or `PROVISION_THREAD_FALLBACK` in the team package (prefer removing from live README too; do not leave “drain-only” wording).
- `WORKFLOWS` never lists both an old and new provisioning type in the same revision.
- API has no in-process provision/deprovision fallback.
- Team tests updated and green for the new surface; no tests encode a drain or dual-path success case.
