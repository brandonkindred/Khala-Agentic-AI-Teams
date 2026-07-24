# DevOps BaseTeamLead Closeout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fold devops Phase 5 (completion package + deliver/merge) into the outer `_run_gated_phases` sequence and route merge failure through `build_team_failure_result`, finishing the BaseTeamLead shared-hooks migration while preserving behavior.

**Architecture:** Extract the current post-sequencer Phase 5 block into `_phase5_completion_deliver`, append it to the existing outer `_run_gated_phases` list, and return merge failures via `build_team_failure_result`. On success the phase fills a nonlocal `completion` and returns `None`; a thin `DevOpsTeamResult(success=True, …)` stays after the sequencer. `DevOpsTeamLeadAgent` remains on `TeamLeadSharedState` with unbound helpers.

**Tech Stack:** Python 3.10, pytest, Ruff

**Spec:** `docs/superpowers/specs/2026-07-24-devops-baseteamlead-closeout-design.md`

**Worktree:** `.worktrees/issue-2015-devops-baseteamlead-closeout` on branch `refactor/2015-devops-baseteamlead-closeout`

## Global Constraints

- Behavior-preserving refactor only — merge-failure / success payloads and side-effect order must match today's orchestrator.
- Stay on `TeamLeadSharedState`; do **not** subclass `BaseTeamLead`.
- Invocation style: unbound `BaseTeamLead._run_gated_phases(self, …)` (same as Phases 1–4).
- Do **not** use `copy_development_result_fields` (code-v2-only fields).
- Do **not** migrate other hand-built early-return `DevOpsTeamResult` sites in Phases 1–4.
- Existing tests pass unchanged — do not edit `test_devops_team.py` or `test_devops_debug_patch.py`.
- 90% coverage floor on `devops_team/orchestrator.py`; `make lint` / targeted pytest from `backend/`.
- Design-by-Contract: document Preconditions/Postconditions on `_phase5_completion_deliver`.
- Never reference GitHub issue numbers in code, comments, docs, or commit messages.

## File Structure

| Path | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/devops_team/orchestrator.py` | Import `build_team_failure_result`; extract `_phase5_completion_deliver`; wire into outer sequencer; thin success return after sequencer |
| `backend/agents/software_engineering_team/tests/test_devops_team.py` | Unchanged — regression suite (incl. `test_delivery_merge_failure_blocks`) |
| `backend/agents/software_engineering_team/tests/test_devops_debug_patch.py` | Unchanged — regression suite |

---

### Task 1: Extract Phase 5 onto gated phases + shared failure envelope

**Files:**
- Modify: `backend/agents/software_engineering_team/devops_team/orchestrator.py` (import ~line 27; shared outputs ~629–634; sequencer + Phase 5 ~912–1032)
- Test: `backend/agents/software_engineering_team/tests/test_devops_team.py`
- Test: `backend/agents/software_engineering_team/tests/test_devops_debug_patch.py`

**Interfaces:**
- Consumes: `BaseTeamLead._run_gated_phases`; `build_team_failure_result`; existing Phase 1–4 callables; closed-over `repo_path`, `task_spec`, `write_changes`, `aggregated_artifacts`, `quality_gates`, `iac_result`, `cicd_result`, `deploy_result`
- Produces: `_phase5_completion_deliver() -> Optional[DevOpsTeamResult]`; nonlocal `completion` for the thin success return; outer sequencer list including Phase 5

- [ ] **Step 1: Confirm baseline green**

From the worktree's `backend/` directory (reuse the main-repo venv if the worktree has no `.venv`):

```bash
PY=/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-2015-devops-baseteamlead-closeout/backend
$PY -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py \
  agents/software_engineering_team/tests/test_devops_debug_patch.py \
  -q
```

Expected: all tests passed (currently ~133; exact count may drift — zero failures is the gate).

- [ ] **Step 2: Import `build_team_failure_result`**

Change the `team_lead_base` import from:

```python
from software_engineering_team.shared.team_lead_base import BaseTeamLead, TeamLeadSharedState
```

to:

```python
from software_engineering_team.shared.team_lead_base import (
    BaseTeamLead,
    TeamLeadSharedState,
    build_team_failure_result,
)
```

- [ ] **Step 3: Add shared `completion` binding next to other Phase outputs**

In `_run_pipeline`, change the shared-outputs block from:

```python
        # Phase outputs shared with Phase 4+ (set by the gated phase callables).
        iac_result: Any = None
        cicd_result: Any = None
        deploy_result: Any = None
        aggregated_artifacts: Dict[str, str] = {}
        quality_gates: Dict[str, str] = {}
```

to:

```python
        # Phase outputs shared with Phase 4+ (set by the gated phase callables).
        iac_result: Any = None
        cicd_result: Any = None
        deploy_result: Any = None
        aggregated_artifacts: Dict[str, str] = {}
        quality_gates: Dict[str, str] = {}
        completion: Any = None  # filled by Phase 5 on success
```

- [ ] **Step 4: Replace the outer sequencer call + inline Phase 5 with Phase 5 callable**

Delete the current outer sequencer call that lists Phases 1–4 **and** the entire inline Phase 5 block through the final `return DevOpsTeamResult(success=True, …)` (from the comment `# Consume BaseTeamLead's gate-based phase sequencer…` through the end of `_run_pipeline`).

Insert this in its place (Phase 1–4 nested defs above are unchanged):

```python
        def _phase5_completion_deliver() -> Optional[DevOpsTeamResult]:
            """Phase 5: completion package assembly + deliver/merge.

            Preconditions: Phases 1–4 returned ``None``; ``quality_gates``,
              ``aggregated_artifacts``, and Phase 2 results are set (artifacts may
              be empty).
            Postconditions: on merge failure returns a failed ``DevOpsTeamResult``
              via ``build_team_failure_result`` with the blocked completion
              package; otherwise assigns nonlocal ``completion`` (completed status,
              git ops, handoff, quality gates) and returns ``None`` so the thin
              success envelope after the sequencer runs.
            """
            nonlocal completion

            # Phase 5: commit, merge, release readiness
            self._report_status(
                "phase5",
                detail="DevOps team pipeline: phase 5 - completion package assembly",
            )
            doc = self.doc_runbook_agent.run(
                DocumentationRunbookInput(
                    task_id=task_spec.task_id,
                    task_title=task_spec.title,
                    artifacts=aggregated_artifacts,
                    quality_gates=quality_gates,
                    notes=[iac_result.summary, cicd_result.summary, deploy_result.summary],
                )
            )

            completion = doc.completion_package
            completion.acceptance_criteria_trace = [
                CriterionTrace(
                    criterion=c,
                    implementation_refs=sorted(aggregated_artifacts.keys()),
                    tests=[{"validation": "pass"}],
                )
                for c in task_spec.acceptance_criteria
            ]
            completion.release_readiness = ReleaseReadiness(
                deployment_strategy=deploy_result.strategy
                or task_spec.constraints.deployment.strategy
                or "rolling",
                rollback_available=bool(deploy_result.rollback_plan),
                alerting_configured=True,
                required_approvals=["manual_prod_approval"]
                if "production" in task_spec.platform_scope.environments
                else [],
                runtime_verification_checklist=[
                    "deployment_rollout_status",
                    "service_health",
                    "alert_health",
                ],
            )
            # Deliver the artifacts for real via the shared inline-merge helper and
            # report the actual outcome (real branch, commit SHA, merge status) rather
            # than fabricated placeholders. A model-only run (write_changes=False) does
            # no git work, so the neutral default honestly reports "nothing delivered".
            git_ops = GitOperationsMetadata()
            if write_changes and aggregated_artifacts:
                deliver_result = deliver_inline_merge(
                    task_id=task_spec.task_id,
                    repo_path=repo_path,
                    deliver_files=aggregated_artifacts,
                    summary=f"implement task [{task_spec.task_id}]",
                    task_title=task_spec.title,
                    commit_msg_template=DEVOPS_DELIVER_COMMIT_MSG_TEMPLATE,
                    ops=_git_ops(),
                    logger=logger,
                )
                # deliver_inline_merge leaves development checked out at the merged
                # commit. merge_branch fast-forwards (development never advanced since
                # the branch was cut), so this single HEAD SHA is the honest identifier
                # for both the delivered commit and the merge result.
                head_ok, head_sha = get_head_sha(repo_path)
                sha = head_sha if head_ok else ""
                commit_msg = (
                    deliver_result.commit_messages[0]
                    if deliver_result.commit_messages
                    else f"feat(devops): implement task [{task_spec.task_id}]"
                )
                if not deliver_result.merged:
                    return build_team_failure_result(
                        DevOpsTeamResult,
                        deliver_result.summary or "DevOps delivery merge failed",
                        completion_package=DevOpsCompletionPackage(
                            task_id=task_spec.task_id,
                            status="blocked",
                            files_changed=sorted(aggregated_artifacts.keys()),
                            quality_gates=quality_gates,
                            git_operations=GitOperationsMetadata(
                                branch_created=deliver_result.branch_name,
                                commits=[GitCommitMetadata(hash="", message=commit_msg)],
                                merge=GitMergeMetadata(
                                    target_branch=DEVELOPMENT_BRANCH,
                                    strategy="merge",
                                    merge_commit_hash="",
                                    status="failed",
                                ),
                            ),
                            notes=[deliver_result.summary],
                        ),
                    )
                git_ops = GitOperationsMetadata(
                    branch_created=deliver_result.branch_name,
                    commits=[GitCommitMetadata(hash=sha, message=commit_msg)],
                    merge=GitMergeMetadata(
                        target_branch=DEVELOPMENT_BRANCH,
                        strategy="merge",
                        merge_commit_hash=sha,
                        status="merged",
                    ),
                )
            completion.git_operations = git_ops
            completion.handoff = HandoffInfo(
                prod_approval_required="production" in task_spec.platform_scope.environments,
                runbook_updated=bool(doc.files),
            )
            completion.status = "completed"
            completion.quality_gates = quality_gates
            return None

        # Consume BaseTeamLead's gate-based phase sequencer without inheriting
        # the code-v2 BaseTeamLead constructor (DevOps uses TeamLeadSharedState).
        early_exit = BaseTeamLead._run_gated_phases(
            self,
            [
                _phase1_intake_clarify,
                _phase2_parallel_design,
                _phase3_branch_write,
                _phase4_validation_review,
                _phase5_completion_deliver,
            ],
        )
        if early_exit is not None:
            return early_exit

        assert completion is not None  # phase 5 success path always assigns it
        return DevOpsTeamResult(success=True, iterations=1, completion_package=completion)
```

Critical details for the implementer:

1. Do not rename or reorder merge-failure package fields — only switch construction to `build_team_failure_result`.
2. Do not subclass `BaseTeamLead` or change Phases 1–4 bodies.
3. `completion = doc.completion_package` inside the phase (with `nonlocal completion`) must assign the outer binding the thin success return reads.
4. Keep the existing Phase 5 comments that explain deliver/merge SHA honesty.

- [ ] **Step 5: Run devops regression tests with coverage**

```bash
PY=/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-2015-devops-baseteamlead-closeout/backend
$PY -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py \
  agents/software_engineering_team/tests/test_devops_debug_patch.py \
  -q --cov=software_engineering_team.devops_team.orchestrator --cov-report=term-missing
```

Expected: all passed; orchestrator line coverage ≥ 90%. Especially confirm `test_delivery_merge_failure_blocks` still passes.

- [ ] **Step 6: Lint the touched file**

Prefer the worktree's `backend/.venv` if present; otherwise the main-repo venv:

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-2015-devops-baseteamlead-closeout/backend
RUFF=/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/ruff
$RUFF check agents/software_engineering_team/devops_team/orchestrator.py
$RUFF format --check agents/software_engineering_team/devops_team/orchestrator.py
```

Expected: both commands exit 0. If format check fails, run `$RUFF format agents/software_engineering_team/devops_team/orchestrator.py` and re-check.

- [ ] **Step 7: Commit**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-2015-devops-baseteamlead-closeout
git add backend/agents/software_engineering_team/devops_team/orchestrator.py
git commit --trailer "Co-authored-by: Cursor <cursoragent@cursor.com>" -m "$(cat <<'EOF'
Refactor devops Phase 5 onto BaseTeamLead gated sequencing.

EOF
)"
```

Do not stage `backend/.venv` (symlink) unless intentionally created. Spec/plan under `docs/superpowers/` are gitignored — force-add only if deliberately documenting on the branch.

---

## Spec coverage (self-review)

| Spec requirement | Task / step |
|---|---|
| Phase 5 on outer `_run_gated_phases` | Task 1 Step 4 |
| Stay on `TeamLeadSharedState` (not subclass) | Global Constraints + Task 1 Step 4 |
| Merge failure via `build_team_failure_result` | Task 1 Steps 2, 4 |
| Success via nonlocal `completion` + thin envelope | Task 1 Steps 3–4 |
| No `copy_development_result_fields` | Global Constraints |
| Exact payloads / side-effect order | Task 1 Step 4 (copied body) + Step 5 regression |
| Existing tests unchanged / pass | Task 1 Steps 1, 5 |
| 90% coverage + lint | Task 1 Steps 5–6 |
| No issue numbers in commit | Task 1 Step 7 message |
| Other Phase 1–4 failure sites untouched | Global Constraints |

No placeholders. Single subsystem — one plan.
