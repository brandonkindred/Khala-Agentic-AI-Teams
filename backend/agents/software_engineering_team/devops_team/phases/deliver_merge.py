"""Phase 5: completion package assembly + deliver/merge (devops_team).

Split out of ``orchestrator.py``. ``doc_runbook_agent`` and ``git_ops`` are
injected explicitly (rather than reached via ``self``) so tests can keep
monkeypatching the same collaborators/module globals as before.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from shared.git.branch_utils import make_branch_suffix
from shared.git.git_utils import DEVELOPMENT_BRANCH, clean_untracked_files
from software_engineering_team.shared.deliver_utils import (
    DeliverGitOps,
    deliver_inline_merge,
    prepare_handoff_branch,
)
from software_engineering_team.shared.team_lead_base import build_team_failure_result

from ..doc_runbook_agent import DocumentationRunbookAgent, DocumentationRunbookInput
from ..models import (
    CriterionTrace,
    DevOpsCompletionPackage,
    DevOpsTaskSpec,
    DevOpsTeamResult,
    GitCommitMetadata,
    GitMergeMetadata,
    GitOperationsMetadata,
    HandoffInfo,
    ReleaseReadiness,
)

logger = logging.getLogger(__name__)

# Commit-message template for the shared deliver helper. ``deliver_inline_merge``
# calls ``template.format(scope=..., summary=...)``; only ``{summary}`` is used
# here (``str.format`` ignores the unreferenced ``scope`` kwarg).
DEVOPS_DELIVER_COMMIT_MSG_TEMPLATE = "feat(devops): {summary}"

# Fallback runtime verification checklist used when the deployment-strategy
# agent's output carries no health checks of its own; the required-approval
# name for production deploys. Named so Phase 5's ReleaseReadiness assembly
# has a single, reusable source for these defaults instead of inline literals.
DEFAULT_RUNTIME_CHECKS = (
    "deployment_rollout_status",
    "service_health",
    "alert_health",
)
PROD_APPROVAL = "manual_prod_approval"


def criterion_traces_from_phase4(
    criteria: List[str],
    acceptance_trace: List[Dict[str, object]],
    artifact_keys: List[str],
) -> List[CriterionTrace]:
    """Map acceptance criteria onto Phase 4 validation evidence.

    Preconditions:
        - ``criteria`` is an iterable of criterion strings (may be empty).
        - ``acceptance_trace`` is an iterable of dict-like Phase 4 entries
          (may be empty); non-dict entries are ignored.
        - ``artifact_keys`` is an iterable of artifact path strings used as
          fallback ``implementation_refs`` when no Phase 4 match exists.

    Postconditions:
        - Returns one ``CriterionTrace`` per entry in ``criteria``, in order.
        - A Phase 4 match (first entry whose ``criterion`` string-equals the
          criterion) supplies coerced ``implementation_refs`` and ``tests``.
        - Unmatched criteria get ``implementation_refs=sorted(artifact_keys)``
          and ``tests=[]``.
        - Never invents a fabricated ``{"validation": "pass"}`` entry.
    """
    by_criterion: Dict[str, Dict[str, object]] = {}
    for entry in acceptance_trace:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("criterion", ""))
        if key and key not in by_criterion:
            by_criterion[key] = entry

    fallback_refs = sorted(artifact_keys)
    traces: List[CriterionTrace] = []
    for criterion in criteria:
        match = by_criterion.get(criterion)
        if match is None:
            traces.append(
                CriterionTrace(
                    criterion=criterion,
                    implementation_refs=list(fallback_refs),
                    tests=[],
                )
            )
            continue

        raw_refs = match.get("implementation_refs", [])
        refs = [str(r) for r in raw_refs] if isinstance(raw_refs, list) else []

        raw_tests = match.get("tests", [])
        tests: List[Dict[str, str]] = []
        if isinstance(raw_tests, list):
            for item in raw_tests:
                if isinstance(item, dict):
                    tests.append({str(k): str(v) for k, v in item.items()})

        traces.append(
            CriterionTrace(
                criterion=criterion,
                implementation_refs=refs,
                tests=tests,
            )
        )
    return traces


@dataclass(frozen=True)
class Phase5DeliverMergeResult:
    """Outcome of Phase 5 (completion package assembly + deliver/merge)."""

    completion: Optional[DevOpsCompletionPackage] = None
    blocked_result: Optional[DevOpsTeamResult] = None


def run_phase5_deliver_merge(
    *,
    task_spec: DevOpsTaskSpec,
    repo_path: Path,
    quality_gates: Dict[str, str],
    acceptance_trace: List[Dict[str, object]],
    aggregated_artifacts: Dict[str, str],
    iac_result: Any,
    cicd_result: Any,
    deploy_result: Any,
    write_changes: bool,
    doc_runbook_agent: DocumentationRunbookAgent,
    git_ops: DeliverGitOps,
    get_head_sha: Callable[[Path], Tuple[bool, str]],
    merge_to_development: bool = True,
) -> Phase5DeliverMergeResult:
    """Phase 5: completion package assembly + deliver/merge.

    Preconditions: Phases 1-4 returned ``None``; ``quality_gates``,
      ``acceptance_trace``, ``aggregated_artifacts``, and ``iac_result``/
      ``cicd_result``/``deploy_result`` from Phase 2's parallel design
      fan-out are set (their ``summary`` attributes are used for runbook
      notes; artifacts/trace may be empty).
    Postconditions: on merge failure returns ``blocked_result`` set to a
      failed ``DevOpsTeamResult`` via ``build_team_failure_result`` with the
      blocked completion package; otherwise returns ``completion``
      (completed status, git ops, handoff, quality gates) with
      ``blocked_result=None``. When ``merge_to_development`` is ``False``,
      the feature branch is committed and left in place (not merged or
      deleted) for external review, and ``completion.git_operations.merge``
      is ``None`` rather than fabricated merge metadata. Before delivering,
      untracked files are cleaned from ``repo_path`` so Phase 4's
      validation/execution tool side effects (e.g. a Terraform provider lock
      file from ``terraform init``) don't get swept into the delivered commit
      alongside ``aggregated_artifacts``; if that cleanup itself fails,
      delivery is blocked (``blocked_result`` set) rather than proceeding with
      an unverified working tree -- a failed clean is exactly the situation
      this exists to protect against, so it must not be treated as best-effort.
    """
    doc = doc_runbook_agent.run(
        DocumentationRunbookInput(
            task_id=task_spec.task_id,
            task_title=task_spec.title,
            artifacts=aggregated_artifacts,
            quality_gates=quality_gates,
            notes=[iac_result.summary, cicd_result.summary, deploy_result.summary],
        )
    )

    completion = doc.completion_package
    completion.acceptance_criteria_trace = criterion_traces_from_phase4(
        list(task_spec.acceptance_criteria),
        acceptance_trace,
        list(aggregated_artifacts.keys()),
    )
    completion.release_readiness = ReleaseReadiness(
        deployment_strategy=deploy_result.strategy
        or task_spec.constraints.deployment.strategy
        or "rolling",
        rollback_available=bool(deploy_result.rollback_plan),
        alerting_configured=bool(deploy_result.alerting_configured),
        required_approvals=[PROD_APPROVAL]
        if "production" in task_spec.platform_scope.environments
        else [],
        runtime_verification_checklist=list(getattr(deploy_result, "health_checks", []))
        or list(DEFAULT_RUNTIME_CHECKS),
    )
    # Deliver the artifacts for real via the shared deliver helper and report
    # the actual outcome (real branch, commit SHA, merge status) rather than
    # fabricated placeholders. A model-only run (write_changes=False) does no
    # git work, so the neutral default honestly reports "nothing delivered".
    # merge_to_development=False (the coding-team handoff mode) commits the
    # feature branch and leaves it in place for external Tech Lead review
    # instead of merging/deleting it — deliver_inline_merge cannot run from a
    # per-task git worktree (merge_branch requires checking out development,
    # which git refuses while it's attached in another worktree).
    git_operations = GitOperationsMetadata()
    if write_changes and aggregated_artifacts:
        # Phase 4's validation/execution tools (e.g. `terraform init` leaving
        # `.terraform.lock.hcl`) can leave untracked files in the working tree.
        # deliver_inline_merge/prepare_handoff_branch both commit via
        # `git add -A`, which would otherwise sweep those tool-created files
        # into the delivered commit even though they were never part of
        # aggregated_artifacts, completion.files_changed, or the internal
        # security/change reviews.
        clean_ok, clean_msg = clean_untracked_files(repo_path)
        if not clean_ok:
            # A failed cleanup must block delivery rather than merely warn: the whole
            # point of this call is to keep untracked validation-tool output out of the
            # commit deliver_inline_merge/prepare_handoff_branch are about to make via
            # `git add -A`. Continuing anyway would let exactly the leftover this exists
            # to catch slip into the delivered commit unreviewed.
            failure_summary = f"Failed to clean untracked validation artifacts: {clean_msg}"
            logger.error("[%s] Deliver: %s", task_spec.task_id, failure_summary)
            return Phase5DeliverMergeResult(
                completion=completion,
                blocked_result=build_team_failure_result(
                    DevOpsTeamResult,
                    failure_summary,
                    completion_package=DevOpsCompletionPackage(
                        task_id=task_spec.task_id,
                        status="blocked",
                        files_changed=[],
                        quality_gates=quality_gates,
                        git_operations=GitOperationsMetadata(),
                        notes=[failure_summary],
                    ),
                ),
            )
        if merge_to_development:
            deliver_result = deliver_inline_merge(
                task_id=task_spec.task_id,
                repo_path=repo_path,
                deliver_files=aggregated_artifacts,
                summary=f"implement task [{task_spec.task_id}]",
                task_title=task_spec.title,
                commit_msg_template=DEVOPS_DELIVER_COMMIT_MSG_TEMPLATE,
                ops=git_ops,
                logger=logger,
            )
        else:
            # Recompute the same branch suffix deliver_inline_merge would use
            # internally and pass it explicitly so prepare_handoff_branch
            # reuses/checks out an existing branch (e.g. on a revision round)
            # instead of deleting and recreating it.
            deliver_result = prepare_handoff_branch(
                task_id=task_spec.task_id,
                repo_path=repo_path,
                deliver_files=aggregated_artifacts,
                summary=f"implement task [{task_spec.task_id}]",
                task_title=task_spec.title,
                feature_branch_name=(
                    f"feature/{make_branch_suffix(task_spec.task_id, task_spec.title)}"
                ),
                commit_msg_template=DEVOPS_DELIVER_COMMIT_MSG_TEMPLATE,
                ops=git_ops,
                logger=logger,
            )
        # deliver_inline_merge leaves development checked out at the merged
        # commit; prepare_handoff_branch leaves the feature branch checked
        # out at its tip. Either way this single HEAD SHA is the honest
        # identifier for the delivered commit.
        head_ok, head_sha = get_head_sha(repo_path)
        sha = head_sha if head_ok else ""
        commit_msg = (
            deliver_result.commit_messages[0]
            if deliver_result.commit_messages
            else f"feat(devops): implement task [{task_spec.task_id}]"
        )
        delivered_ok = (
            deliver_result.merged if merge_to_development else deliver_result.branch_ready
        )
        if not delivered_ok:
            failure_summary = deliver_result.summary or (
                "DevOps delivery merge failed"
                if merge_to_development
                else "DevOps delivery branch preparation failed"
            )
            return Phase5DeliverMergeResult(
                completion=completion,
                blocked_result=build_team_failure_result(
                    DevOpsTeamResult,
                    failure_summary,
                    completion_package=DevOpsCompletionPackage(
                        task_id=task_spec.task_id,
                        status="blocked",
                        files_changed=sorted(aggregated_artifacts.keys()),
                        quality_gates=quality_gates,
                        git_operations=GitOperationsMetadata(
                            branch_created=deliver_result.branch_name,
                            commits=[GitCommitMetadata(hash="", message=commit_msg)],
                            merge=(
                                GitMergeMetadata(
                                    target_branch=DEVELOPMENT_BRANCH,
                                    strategy="merge",
                                    merge_commit_hash="",
                                    status="failed",
                                )
                                if merge_to_development
                                else None
                            ),
                        ),
                        notes=[failure_summary],
                    ),
                ),
            )
        if merge_to_development:
            merge_status = "merged" if head_ok else "merged_sha_unknown"
            if not head_ok:
                completion.notes.append(
                    "Merge succeeded but HEAD SHA could not be read after merge; "
                    "commit hash unknown."
                )
            merge_metadata: Optional[GitMergeMetadata] = GitMergeMetadata(
                target_branch=DEVELOPMENT_BRANCH,
                strategy="merge",
                merge_commit_hash=sha,
                status=merge_status,
            )
        else:
            if not head_ok:
                completion.notes.append(
                    "Branch prepared but HEAD SHA could not be read; commit hash unknown."
                )
            merge_metadata = None
        git_operations = GitOperationsMetadata(
            branch_created=deliver_result.branch_name,
            commits=[GitCommitMetadata(hash=sha, message=commit_msg)],
            merge=merge_metadata,
        )
    completion.git_operations = git_operations
    completion.handoff = HandoffInfo(
        prod_approval_required="production" in task_spec.platform_scope.environments,
        runbook_updated=bool(doc.files),
    )
    completion.status = "completed"
    completion.quality_gates = quality_gates
    return Phase5DeliverMergeResult(completion=completion, blocked_result=None)
