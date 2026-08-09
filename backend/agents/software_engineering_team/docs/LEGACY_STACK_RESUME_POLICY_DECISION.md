# Decision: Resume Policy for Legacy Stacks and Legacy HITL Reasons

## Status

**Decided.** This document states how persisted coding-team jobs that
still carry pre-v2 shapes — legacy stack aliases, or a task missing
`target_team`, or a legacy free-text HITL `user_decision` reason — are to
be handled once the repair/fallback code that tolerates those shapes is
removed. It is a policy decision only; it does not itself delete any
repair code.

## Background

Three related repair/fallback mechanisms exist purely to tolerate
persisted job state from before `frontend_v2`/`backend_v2` routing and
structured HITL decisions:

1. **Legacy stack aliases.** `_LEGACY_BACKEND_STACK_ALIASES`
   (`team_routing.py`) = `{default, senior_software_engineer,
   senior_software_engineer_legacy, software_engineer}`.
   `_legacy_stack_key` normalizes a stack name for comparison.
   `_v2_team_kind_for_stack` falls back to `"backend"` for a
   legacy-aliased stack. `_ensure_target_team_stack_specs` (invoked from
   the coding-team orchestrator on resume) rewrites a persisted legacy
   stack entry to the canonical `backend_v2` spec.
2. **Missing `target_team` fallback.** The Tech Lead's
   `run_plan_to_task_graph` (`tech_lead_agent/agent.py`) falls back
   through `team` → `stack` → `assignee_stack` when a task lacks
   `target_team`, and constructs a `{"name": "default", ...}` stub stack
   when the LLM call fails or no stacks are present — the same
   `"default"` name caught by (1).
3. **Legacy HITL reason shape.** `swarm_review._add_legacy_reason`
   handles `revision_feedback` entries with `source == "user_decision"`
   that lack a structured `decisions` list, instead parsing `"- q → a"`
   bullets out of a free-text `reason` string.

**Persistence.** A single Postgres `jobs` table
(`backend/job_service/postgres.py`, primary key `(team, job_id)`) stores
job state in a JSONB `data` column, namespaced by `team`. There is no
`schema_version` field — legacy vs. current shape is only detectable by
field presence/absence inside `data`. The repair functions above can fire
against records under **two different `team` keys**, both of which carry
`task_graph_snapshot`/`stack_specs`/per-task `revision_feedback` in the
same shape:

- `team = 'coding_team'` — standalone `/api/coding-team` jobs. Resume
  happens via a Temporal `submit_answers` signal that re-invokes the
  pipeline activity, which re-reads the job record and restores
  `task_graph_snapshot`/`stack_specs`. Status values in use: `pending`,
  `running`, `waiting_for_user` (paused for HITL — still
  non-terminal/resumable, per `job_store.NON_TERMINAL_STATUSES`),
  `completed`, `failed`, `cancelled` (terminal — no resume/retry endpoint
  exists for a `coding_team` job once it reaches one of these).
- `team = 'software_engineering_team'` — the full four-phase `run_team`
  pipeline. Its execution phase delegates to the same coding-team
  orchestrator (`orchestrator._run_coding_and_finalize` →
  `run_coding_team_orchestrator`), but with `update_job_fn`/`get_job_fn`
  bound to the *SE* job store, so the coding-team snapshot is persisted
  under the SE job's own record instead of a separate `coding_team` row.
  This namespace has **three** resume paths, not one: `POST
  /run-team/{job_id}/resume` (status in `RESUMABLE_STATUSES` — `pending`,
  `running`, `agent_crash`, `failed`, `waiting_for_user`); `POST
  /run-team/{job_id}/retry-failed`, which has no status gate beyond
  rejecting `running` and instead only requires a non-empty
  `failed_tasks` list — so a `completed` or `cancelled` SE job that ended
  with lingering `failed_tasks` remains retry-eligible indefinitely, not
  just while non-terminal; and `POST
  /run-team/{job_id}/resume-after-llm-check`, gated only on `status ==
  "paused_llm_connectivity"` (no `failed_tasks` requirement). (`POST
  /run-team/{job_id}/restart` is not a resume path for this purpose:
  `reset_job` fully replaces the job record with a clean payload that
  carries no `task_graph_snapshot`/`stack_specs`, so a restarted job
  cannot carry forward a legacy shape — but it is only reachable once a
  job's status is in `RESTARTABLE_STATUSES`, which `completed_with_failures`
  is not, so cancel-then-restart is sometimes the path to it; see the
  pre-deploy check below.)

## Decision

**Fail-fast, not one-shot migrate, for both the legacy stack shape and
the legacy HITL reason shape.**

Once the repair/fallback code above is removed:

- Resuming a job whose persisted `stack_specs` contains a legacy alias
  name, or whose `task_graph_snapshot` has a task without `target_team`,
  raises a clear, actionable error identifying the job id and the
  offending field — it does not silently rewrite to `backend_v2` or fall
  back through `team`/`stack`/`assignee_stack`.
- Resuming a job whose `revision_feedback` has a `user_decision` entry
  lacking `decisions` raises the same class of error instead of being
  auto-parsed from `reason` text.
- Operator response to the error: cancel/fail the job and have the
  requester resubmit through the current pipeline. These are short-lived
  plan → execute → review jobs, not long-lived data, so resubmission is
  cheap relative to building and maintaining a migration path for them.

## Rationale

- **The repair code is resume-time patching, not a batch job.** It is
  spread across `team_routing.py` and `tech_lead_agent/agent.py`
  specifically because it repairs one job at a time as it's resumed. A
  dedicated one-shot migration script would re-implement the same logic
  in a throwaway tool, doubling the maintenance surface this cleanup
  exists to close.
- **Most jobs are short-lived, and the audit below is scoped to catch the
  exception.** Plan → execute → review pipelines don't sit in
  `pending`/`running` for long, and even a `waiting_for_user` pause is
  bounded by how long it takes an operator to answer — but an SE
  `run_team` job that ended `completed`/`cancelled` with lingering
  `failed_tasks` stays retry-eligible via `/retry-failed` indefinitely,
  with no time bound. The pre-deploy audit below is written to include
  that case explicitly rather than assuming all resumable jobs are
  non-terminal; a fail-fast error still beats a permanent migration path
  for the (expected to be rare) jobs it flags.
- **Fail loud, not silent.** A clear, field-identifying error beats
  either silently coercing the data or silently refusing without a
  diagnostic — an operator needs to know *which* job and *which* field
  tripped the check to act on it, consistent with this repository's "no
  silent data loss" expectation for state-carrying refactors.
- **Free-text HITL parsing isn't worth preserving.**
  `_add_legacy_reason`'s bullet extraction from free text is heuristic by
  construction; once structured `decisions` is universal there is no
  value in keeping a permanent fallback parser for the old shape.

## Pre-deploy job-store check

Before deploying the repair-code removal, run a one-time audit against
the `jobs` table covering **both** namespaces, each restricted to the
rows that namespace's own API can still resume:

- `coding_team`: `status IN ('pending', 'running', 'waiting_for_user')` —
  its only resume path is the HITL signal on a non-terminal job.
- `software_engineering_team`: the `/resume`-eligible statuses
  (`pending`, `running`, `agent_crash`, `failed`, `waiting_for_user`),
  **or** `paused_llm_connectivity` (resumable via `POST
  /run-team/{job_id}/resume-after-llm-check`, which only checks the
  status — not `failed_tasks` — before retrying), **or** any status with
  a non-empty `failed_tasks` array (the `/retry-failed` path, which is
  not gated by status beyond excluding `running`).

```sql
SELECT team, job_id, status
FROM jobs
WHERE (
    (team = 'coding_team' AND status IN ('pending', 'running', 'waiting_for_user'))
    OR (team = 'software_engineering_team' AND (
      status IN ('pending', 'running', 'agent_crash', 'failed',
                 'waiting_for_user', 'paused_llm_connectivity')
      OR jsonb_array_length(COALESCE(data->'failed_tasks', '[]'::jsonb)) > 0
    ))
  )
  AND (
    -- (a) a stack_specs entry uses a legacy alias name, normalized the same
    -- way _legacy_stack_key does (strip, lowercase, '-'/' ' -> '_'). Python's
    -- .strip() removes all whitespace (tabs/newlines included), not just
    -- spaces, so the leading/trailing trim uses \s+ rather than trim() (which
    -- is space-only in Postgres) — variants like " Senior Software Engineer ",
    -- "\tsenior-software-engineer\n" are all caught
    EXISTS (
      SELECT 1 FROM jsonb_array_elements(data->'stack_specs') AS s
      WHERE lower(regexp_replace(
        regexp_replace(s->>'name', '^\s+|\s+$', '', 'g'), '[- ]', '_', 'g'
      )) IN (
        'default', 'senior_software_engineer',
        'senior_software_engineer_legacy', 'software_engineer'
      )
    )
    -- (b) a task in the snapshot is missing target_team
    OR EXISTS (
      SELECT 1 FROM jsonb_array_elements(data->'task_graph_snapshot') AS t
      WHERE NOT (t ? 'target_team') OR t->>'target_team' IS NULL OR t->>'target_team' = ''
    )
    -- (c) a user_decision revision_feedback entry lacks structured decisions;
    -- revision_feedback is a per-task field inside task_graph_snapshot, not a
    -- top-level data key, so it must be reached through the task elements
    OR EXISTS (
      SELECT 1
      FROM jsonb_array_elements(data->'task_graph_snapshot') AS t,
           jsonb_array_elements(COALESCE(t->'revision_feedback', '[]'::jsonb)) AS f
      WHERE f->>'source' = 'user_decision' AND NOT (f ? 'decisions')
    )
  );
```

The query is a point-in-time snapshot, not a standing guarantee: a
`pending` job with an empty `stack_specs`/`task_graph_snapshot` passes it
today but can still be planned by the still-running pre-cleanup code
afterward — e.g. a Tech Lead LLM-call failure produces the
`{"name": "default", ...}` fallback stack — and reach `waiting_for_user`
before the deploy actually lands. Pausing new submissions is not
sufficient by itself: an already-created `pending`/`running` job's
pre-cleanup worker is still free to write a legacy shape into it after
the query returns zero. Treat zero rows as safe only when, from the
moment the query is run until the repair-code-removal build is live:

1. New `coding_team`/`run_team` job submissions are paused (or otherwise
   frozen), **and**
2. Existing `pending`/`running`/`waiting_for_user` workers are drained to
   a terminal state or stopped — not merely left running — so none of
   them can persist a legacy `stack_specs`/`task_graph_snapshot` write
   after the gate.

Re-run the query right before un-freezing submissions/workers if the
deploy window is longer than a few minutes.

- **Zero rows:** safe to deploy.
- **Any rows:** do not deploy the repair-code removal yet. The valid
  recovery path depends on the row's status — `POST
  /run-team/{job_id}/restart` only works for a status already in
  `RESTARTABLE_STATUSES` (`completed`, `failed`, `cancelled`,
  `agent_crash`, `already_complete`), so it is not a universal fix:
  - `coding_team` rows (`pending`/`running`/`waiting_for_user`): let the
    job drain (resume/complete) under the current, pre-cleanup code, or
    cancel it via the existing job-cancel path.
  - `software_engineering_team` rows in `pending`/`running`/
    `waiting_for_user`/`agent_crash`: cancel via `POST
    /run-team/{job_id}/cancel` (allowed for these statuses), which moves
    the job to `cancelled` — then `POST /run-team/{job_id}/restart` is
    valid and drops the legacy snapshot. `failed` is directly
    restartable/resumable as-is.
  - `software_engineering_team` rows sitting in `completed_with_failures`
    (the normal result when some coding-team tasks fail) are **not**
    directly restartable — `restart` 400s outside `RESTARTABLE_STATUSES`.
    Two valid recoveries: run `POST /run-team/{job_id}/retry-failed`
    under the current, pre-cleanup code until `failed_tasks` is empty; or,
    if a task fails deterministically and retry-failed can never clear
    it, `POST /run-team/{job_id}/cancel` — `completed_with_failures` is
    not in `request_cancel`'s terminal-status check either, so cancel
    succeeds and moves the job to `cancelled`, after which `restart` is
    valid and drops the legacy snapshot.
  - `software_engineering_team` rows in `paused_llm_connectivity`: call
    `POST /run-team/{job_id}/resume-after-llm-check` under the current,
    pre-cleanup code (or cancel-then-restart, same as above).

  Re-run the check before deploying.

This check only needs to run once per deploy of the repair-code-removal
work; no recurring migration job or new schema field is introduced.

## Out of scope

- Implementing the repair-code deletions themselves.
- Adding a `schema_version` field or other persistence-format changes
  beyond dropping the legacy fields the repair code targets.
- A reusable/generic migration framework for job JSONB — this decision is
  scoped to the specific legacy shapes above.

## Applies to

Any implementation work that removes `_LEGACY_BACKEND_STACK_ALIASES` /
`_legacy_stack_key` / the repair branches in `_v2_team_kind_for_stack`
and `_ensure_target_team_stack_specs`, the Tech Lead
`team`/`stack`/`assignee_stack` fallback, the `{"name": "default", ...}`
fallback stack, or `swarm_review._add_legacy_reason` must follow the
fail-fast policy and pre-deploy check documented above.
