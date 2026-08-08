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
all coding-team job state in a JSONB `data` column. There is no
`schema_version` field — legacy vs. current shape is only detectable by
field presence/absence inside `data`. Job `status` values in use:
`pending`, `running` (non-terminal, resumable), `completed`, `failed`,
`cancelled` (terminal, never resumed again). Resume happens via a
Temporal `submit_answers` signal that re-invokes the pipeline activity,
which re-reads the job record and restores `task_graph_snapshot`/
`stack_specs` — exactly where the repair functions above fire today.

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
- **Jobs are short-lived.** Plan → execute → review pipelines don't sit
  in `pending`/`running` for long, so a pre-deploy audit for any
  in-flight legacy-shaped job (below) is cheap and should normally find
  zero rows. There is no large body of long-lived legacy data that would
  justify a permanent migration path.
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
the `jobs` table for the coding team's `team` key, restricted to
**non-terminal** rows (`status IN ('pending', 'running')`):

```sql
SELECT job_id, status
FROM jobs
WHERE team = '<coding-team-key>'
  AND status IN ('pending', 'running')
  AND (
    -- (a) a stack_specs entry uses a legacy alias name
    EXISTS (
      SELECT 1 FROM jsonb_array_elements(data->'stack_specs') AS s
      WHERE s->>'name' IN ('default', 'senior_software_engineer',
                            'senior_software_engineer_legacy', 'software_engineer')
    )
    -- (b) a task in the snapshot is missing target_team
    OR EXISTS (
      SELECT 1 FROM jsonb_array_elements(data->'task_graph_snapshot') AS t
      WHERE NOT (t ? 'target_team') OR t->>'target_team' IS NULL OR t->>'target_team' = ''
    )
    -- (c) a user_decision revision_feedback entry lacks structured decisions
    OR EXISTS (
      SELECT 1 FROM jsonb_array_elements(COALESCE(data->'revision_feedback', '[]'::jsonb)) AS f
      WHERE f->>'source' = 'user_decision' AND NOT (f ? 'decisions')
    )
  );
```

- **Zero rows:** safe to deploy.
- **Any rows:** do not deploy the repair-code removal yet. Either let the
  matching jobs drain (resume/complete) under the current, pre-cleanup
  code, or manually cancel them via the existing job-cancel path, then
  re-run the check before deploying.

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
