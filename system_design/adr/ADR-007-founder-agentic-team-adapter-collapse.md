# ADR-007 — Founder target-adapter: collapse agentic-team pipelines onto the three-phase Protocol

- **Status**: Accepted
- **Date**: 2026-07-03
- **Owner**: User Agent Founder team
- **Related**: `backend/agents/agent_team_studio/user_agent_founder/system_design/FEATURE_SPEC_testing_personas.md` (Backend Design section — defines the `TargetTeamAdapter` Protocol)

## Context

The `user_agent_founder` team lets a testing persona autonomously drive a
target team end-to-end. Every target sits behind the `TargetTeamAdapter`
Protocol (`backend/agents/agent_team_studio/user_agent_founder/targets/base.py`), which assumes
a **three-phase** target:

1. **Spec** — generated persona-side, no adapter involvement.
2. **Analysis** — `start_from_spec` → `poll_analysis` → `submit_analysis_answers`,
   surfacing batched multiple-choice questions and producing a `repo_path`
   output that the orchestrator persists and threads into the next phase.
3. **Build** — `start_build(repo_path)` → `poll_build` → `submit_build_answers`,
   again with batched multiple-choice questions.

An **agentic team's test pipeline**
(`backend/agents/agent_team_studio/agentic_team_provisioning`) is shaped differently: a single
linear DAG run (`TestPipelineRun`) that starts from one `initial_input`,
walks its steps in topological order, and occasionally pauses on a `WAIT`
step whose `human_prompt` expects one **free-text** answer posted to the
run's `/input` endpoint. There is no analysis phase and there are no
multiple-choice questions.

This is a structural impedance mismatch, not a cosmetic one. The
persona-drives-any-team flow depends on bridging it, and the bridge —
`AgenticTeamAdapter`
(`backend/agents/agent_team_studio/user_agent_founder/targets/agentic_team.py`) — necessarily
couples the two contracts: a shape change on either side can break the
bridge silently.

Two resolutions were considered:

- **Keep the collapsing adapter**: map the pipeline shape onto the
  three-phase Protocol inside the adapter, accepting the coupling.
- **Generalize the founder Protocol**: introduce a single-phase Protocol
  variant that agentic pipelines fit natively.

## Decision

**Keep the collapsing adapter.** The coupling is accepted deliberately,
documented here as an explicit contract boundary, and guarded by a drift
tripwire test that fails loudly when either side's shape moves.

The collapse rules (implemented in `AgenticTeamAdapter`):

- **Analysis is a no-op pass-through.** `start_from_spec` records the persona
  spec on the adapter and returns a sentinel job id without any HTTP call;
  `poll_analysis` reports immediate completion carrying no phase output. The
  spec reaches `start_build` via the adapter's own `self._spec` — set live by
  `start_from_spec`, or seeded at construction from the persisted `spec_content`
  column when a resumed run skips that call — so the Protocol's `repo_path`
  analysis→build slot is left NULL (an agentic team has no filesystem repo).
  `submit_analysis_answers` is a no-op — the collapsed phase never raises
  questions.
- **Build is the test-pipeline run.** `start_build` POSTs the spec (from
  `self._spec`) as the pipeline's `initial_input` and returns the pipeline
  `run_id`.
- **A WAIT step becomes exactly one free-text question.** `poll_build` maps a
  `waiting_for_input` run with a non-empty `human_prompt` to a single pending
  question with empty `options` (forcing the persona's free-text "other"
  answer). The question id is stable per `(run, step)`; a missing/empty
  `current_step_id` falls back to `"wait"`.
- **The answer resumes the pipeline.** `submit_build_answers` POSTs the free
  text to the run's `/input` endpoint, substituting the placeholder
  `"(no answer provided)"` when the answer is blank so the endpoint's
  min-length validation still passes and the run advances.
- **Terminal statuses are normalized.** Either cancellation spelling from the
  pipeline is rewritten to the canonical `PipelineRunStatus.CANCELLED.value`
  (`"cancelled"`) so the orchestrator's shared exact-string terminal check
  recognizes it.

### Rejected alternative: generalize the Protocol to a single-phase variant

Rejected because the cost lands everywhere and buys nothing functional:

- The orchestrator's phase machinery (`orchestrator.py` — the shared
  `_run_phase` loop, the analysis→build handoff, and the resume path keyed on
  the persisted `analysis_job_id`/`repo_path` checkpoint columns) would need
  a second code path or a phase-count abstraction.
- The existing `SoftwareEngineeringAdapter` and the run store's checkpoint
  columns would need migration to whatever the generalized shape becomes.
- The UI's run/decision timeline assumes the three-phase progression.
- The adapter is shipped, behavior-tested, and required zero orchestrator
  changes — the generalization would deliver the same observable behavior.

**Revisit trigger**: if a *second* single-phase target team appears, the
collapse stops being a one-off and Protocol generalization should be
re-evaluated instead of adding a second collapsing adapter.

### Contract boundary

The exact surface the adapter depends on, in both directions. Changing
anything below requires updating the adapter and the drift tripwire together.

Provisioning → founder (defined in `backend/agents/agent_team_studio/agentic_team_provisioning`):

- `TestPipelineRun` fields the adapter reads: `run_id`, `status`,
  `current_step_id`, `human_prompt`, `error`.
- `PipelineRunStatus` values: `running`, `waiting_for_input`, `completed`,
  `failed`, `cancelled` — every member must be mapped by the adapter.
- Request DTOs: `StartPipelineRunRequest` (`process_id`, `initial_input`) and
  `SubmitPipelineInputRequest` (`input`, min length 1 — satisfied by the
  blank-answer placeholder).
- Routes on the provisioning app: `POST /teams/{team_id}/test-pipeline/runs`,
  `GET /teams/{team_id}/test-pipeline/runs/{run_id}`,
  `POST /teams/{team_id}/test-pipeline/runs/{run_id}/input`, mounted at the
  unified-API prefix `/api/agentic-team-provisioning`.

Founder-internal (defined in `backend/agents/agent_team_studio/user_agent_founder`):

- The `TargetTeamAdapter` Protocol shape — attributes `team_key`/
  `display_name` and the six method signatures. The Protocol is
  `runtime_checkable`, so `isinstance` checks attribute *presence* only;
  signature drift is caught by the tripwire, not by `isinstance`.
- Poll-dict keys the orchestrator consumes: `status`, `waiting_for_answers`,
  `pending_questions`, `_poll_error`, and the terminal status strings
  `completed`/`failed`/`cancelled`. (The agentic adapter does not emit
  `repo_path`; its analysis phase carries no filesystem path.)

## Consequences

- The coupling is accepted and guarded, split across two suites:
  `backend/agents/agent_team_studio/user_agent_founder/tests/test_adapter_agentic_team_contract_drift.py`
  imports the **real** provisioning-side models, DTOs, and app routes and
  fails loudly when the provisioning-side surface or the Protocol signatures
  drift (the behavioral suite in `test_adapter_agentic_team.py` scripts fake
  HTTP responses, so it cannot see real-side drift by itself). The
  founder-internal poll-dict keys and terminal strings are exercised through
  the **real orchestrator** by the behavioral suite's persona-run tests —
  one per terminal outcome (completed, failed, cancelled) plus the
  transient-poll-error retry path — so changing the orchestrator's
  expectations fails there, not in the drift file.
- The former overload of the Protocol's `repo_path` slot to carry a persona
  *spec* for agentic targets has been removed: the agentic adapter threads the
  spec through its own `self._spec` and leaves `repo_path` NULL, so `repo_path`
  now means only a real filesystem path (the software-engineering target). The
  analysis-phase success signal was decoupled from a non-NULL `repo_path`
  (`_run_product_analysis` returns an explicit `(ok, repo_path)`), which is what
  had forced a value into the slot.
- Protocol generalization is deferred, with the revisit trigger above.
- CI caveat: the tripwire lives in the founder test suite, which is not
  currently part of the per-team CI test matrix — it fires wherever the
  founder suite runs (locally and in per-team runs), but not automatically on
  a provisioning-side change until the matrix gains the founder suite.
