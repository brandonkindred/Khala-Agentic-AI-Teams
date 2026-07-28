# Audit: Downstream Consumers of the Code Review Phase's Build/Lint Execution

## Purpose

The code review phase currently runs build verification and linting as
sub-steps before the code-review-agent call (see
[`GATE_DEPENDENCY_GRAPH.md`](GATE_DEPENDENCY_GRAPH.md), which already notes
"Build is not a standalone gate here — it's a sub-step inside Code Review").
A planned change would remove that build/lint execution from the code review
phase. This document is a read-only audit, done ahead of that change, tracing
every place in the codebase that could plausibly depend on the code review
phase specifically being the one that runs build/lint, or on its pass/fail
result being read from that phase's output. It makes no code changes and does
not touch CI's build/lint pipeline.

## Where build/lint currently execute and how the result is captured

`run_code_review_phase_impl` (`shared/phases/review.py:45-187`) runs, in
order, inside a single call:

1. **Build verification** (lines 99-110): `build_ok, build_msg =
   build_verify_fn(repo_path, build_verifier, task_id)`. On failure, appends a
   `ReviewIssue(source="build", severity="critical", ...)`.
2. **Lint** (lines 112-148): if `linting_tool_agent is not None`, runs it via
   `LintToolInput(...)` and, on failure, appends one `ReviewIssue(source="lint",
   ...)` per lint issue.
3. **Code-review-agent step** (lines 150-169): delegates to the shared
   `_code_review_step`.

The phase's `passed` is computed at line 172 as `build_ok and lint_ok and
len(critical_or_high) == 0`, and returned as a `PhaseReviewResult`
(`shared/v2_models.py`) with `passed`, `issues`, `summary`, `phase_name`,
`raw_issue_count`. The `summary` string embeds `build={OK|FAIL}` and
`lint={OK|FAIL}` (lines 174-178) for human-readable logging only.

That `PhaseReviewResult` is converted into a `GateOutcome`
(`review_cycle.py:111-128`) by each team's `_code_review_gate` adapter
(e.g. `backend_code_v2_team/phases/execution.py:170-221`), and consumed by
`review_cycle.py`'s `_run_review_cycles` to drive the
Code-Review→QA→Security retry loop (`shared/phases/execution.py`). Within
that loop, a build or lint failure is indistinguishable from a code-review
finding once it becomes a `ReviewIssue` — it just triggers the same
batch-fix-and-retry path as any other blocking issue.

`build_verify_fn`/`build_verifier` ultimately resolve to
`_run_build_verification` in `build_fix.py` (real `ng build` / `pytest` /
Python syntax check / `docker build`), and `linting_tool_agent` resolves to
`LintingToolAgent` (`linting_tool_agent/agent.py`), which runs `ruff` /
`flake8` / `ng lint` / `eslint` via `linter_runner.py`.

## Merge-gate audit

The only place in the codebase that performs a branch merge for a task, and
the sole caller of `TaskGraphService.mark_branch_merged`, is
`swarm_review.py::_apply_review_decision` (lines 290-346):

```python
elif review.get("approved"):
    ...
    ok, merge_msg = merge_branch(self.path, _orch._feature_branch_name(task), DEVELOPMENT_BRANCH)
    if ok:
        self.graph.mark_branch_merged(task.id)
```

`review` is produced by `_compute_review` (lines 233-288), which calls
`self.tech_lead.run_code_review(...)` — a **fresh Tech Lead LLM call against
the branch diff**, independent of anything computed inside
`run_code_review_phase_impl`. The merge condition is the boolean
`review.get("approved")` only.

A repo-wide search for `build_passed`, `lint_passed`, `build_result`,
`lint_result`, `build_status`, `lint_status`, `can_merge`, `merge_gate`, and
`should_merge` found no occurrences anywhere in merge-decision code. The merge
gate does not read `PhaseReviewResult`, `GateOutcome`, or any field the code
review phase produces.

### Why the v2 "gated execution" pipeline doesn't change this conclusion

The v2 pipeline (`shared/phases/execution.py`'s `run_gated_execution_impl`)
does fold build/lint into a microtask-level `passed` flag that gates
progression to QA/Security/Documentation within that loop, and into the v2
workflow's own `success` flag (`shared/v2_orchestrator.py`). However,
`v2_team_worker.py` always calls the coding-team orchestrator with
`merge_to_development=False`, so the v2 pipeline never merges a branch
itself. It hands off an **unmerged** branch, and the same
`swarm_review.py::_apply_review_decision` gate above re-derives its own
approve/reject verdict from a brand-new Tech Lead diff review — not from the
v2 loop's stored `PhaseReviewResult`/`GateOutcome`. The v2 worker's own
handoff summary is explicit that it "does NOT assert [the issue] was
resolved — the coding-team Tech Lead review verifies the actual diff."

### `ReviewResult.build_ok` / `.lint_ok`

`shared/v2_models.py`'s `ReviewResult` (used by the non-gated `run_review` /
`run_microtask_review` path) has explicit `build_ok: bool` and `lint_ok: bool`
fields separate from `passed`. A repo-wide grep for `.build_ok` and
`.lint_ok` shows they are read **only from test files**
(`tests/test_v2_review_phase.py`, `tests/test_v2_review_shared.py`,
`tests/test_backend_code_v2_team.py`, `tests/test_v2_fe_review_phase.py`,
`tests/test_microtask_review_gates.py`). No production code path outside
`review.py` itself reads these fields.

### Swarm pipeline's separate pre-review quality gate

`swarm_implementation.py`'s task-readiness quality gate runs build/lint
independently via `quality_gate_tools.py`'s `BuildResult`/`LintResult`,
bouncing a task back to `TO_DO` on failure — this runs *before* a task ever
reaches the Tech Lead's code review and is architecturally separate from
`run_code_review_phase_impl`. It does not read anything from the code review
phase's output, so it is unaffected by removing build/lint from that phase.

### DbC (Design by Contract) comments

`run_dbc_comments` (`quality_gate_tools.py`) is confirmed dormant: not
invoked by `orchestrator.py` or `shared/phases/execution.py`, only exercised
from `tests/test_quality_gate_tools.py` (also noted in
`GATE_DEPENDENCY_GRAPH.md`). It has no dependency on build/lint results.

## Audit conclusion

**No downstream consumer depends on the code review phase specifically being
the one that runs build/lint, and nothing reads a build/lint pass/fail
result from that phase's stored output for merge-gating purposes.** The only
consumer of build/lint results is the code review phase's own `passed`
computation, which governs that phase's own retry loop — a coupling that is
entirely internal to the phase being changed. The actual merge decision
(`swarm_review.py::_apply_review_decision`) is driven solely by an
independent Tech Lead diff review and was already decoupled from build/lint
status before this audit.

Because no external dependency was found, no compensating change (e.g.
reading CI's build/lint result directly) is required elsewhere before
build/lint execution can be removed from the code review phase. CI's own
build/lint pipeline is unaffected and out of scope for this audit.
