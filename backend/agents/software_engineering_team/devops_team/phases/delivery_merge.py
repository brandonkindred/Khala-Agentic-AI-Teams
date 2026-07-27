"""Phase 5 delivery/merge (devops_team)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from software_engineering_team.shared.deliver_utils import DeliverGitOps
from software_engineering_team.shared.git_utils import DEVELOPMENT_BRANCH
from software_engineering_team.shared.team_lead_base import build_team_failure_result

from ..models import (
    DevOpsCompletionPackage,
    DevOpsTaskSpec,
    DevOpsTeamResult,
    GitCommitMetadata,
    GitMergeMetadata,
    GitOperationsMetadata,
)


@dataclass(frozen=True)
class DeliveryMergeResult:
    """Outcome of Phase 5 delivery/merge.

    Invariants: ``failure_result is None`` implies ``git_ops`` reflects a
      successful merge (or, when nothing was delivered, the neutral
      "nothing delivered" default) and ``notes`` carries any post-merge
      caveats to append to the completion package; a non-``None``
      ``failure_result`` means the real merge failed and carries the
      blocked ``DevOpsTeamResult``.
    """

    git_ops: GitOperationsMetadata
    notes: List[str] = field(default_factory=list)
    failure_result: Optional[DevOpsTeamResult] = None


def deliver_and_merge(
    *,
    task_spec: DevOpsTaskSpec,
    repo_path: Path,
    aggregated_artifacts: Dict[str, str],
    write_changes: bool,
    quality_gates: Dict[str, str],
    commit_msg_template: str,
    ops: DeliverGitOps,
    deliver_inline_merge: Callable[..., Any],
    get_head_sha: Callable[[Path], Tuple[bool, str]],
    logger: logging.Logger,
) -> DeliveryMergeResult:
    """Deliver aggregated artifacts and merge them into the development branch.

    Preconditions: ``task_spec``/``aggregated_artifacts``/``quality_gates``
      are the pipeline's own Phase 1-4 outputs; ``ops`` bundles this
      pipeline's git callables (so callers can still monkeypatch the
      underlying names on their own module, e.g. ``merge_branch``);
      ``deliver_inline_merge`` and ``get_head_sha`` are passed through by the
      caller (rather than imported fresh here) for the same reason.

    Postconditions: when ``write_changes`` is False or ``aggregated_artifacts``
      is empty, returns the neutral "nothing delivered"
      ``DeliveryMergeResult`` (default ``GitOperationsMetadata()``, no
      failure). Otherwise delivers via ``deliver_inline_merge``; on merge
      failure returns a ``DeliveryMergeResult`` whose ``failure_result`` is
      the blocked ``DevOpsTeamResult`` (status "blocked", ``merge.status ==
      "failed"``); on success returns ``git_ops`` reflecting the real branch,
      commit SHA, and merge status (``"merged"`` or
      ``"merged_sha_unknown"`` when the post-merge HEAD read fails, with a
      matching note).
    """
    git_ops = GitOperationsMetadata()
    if not (write_changes and aggregated_artifacts):
        return DeliveryMergeResult(git_ops=git_ops)

    deliver_result = deliver_inline_merge(
        task_id=task_spec.task_id,
        repo_path=repo_path,
        deliver_files=aggregated_artifacts,
        summary=f"implement task [{task_spec.task_id}]",
        task_title=task_spec.title,
        commit_msg_template=commit_msg_template,
        ops=ops,
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
        return DeliveryMergeResult(
            git_ops=git_ops,
            failure_result=build_team_failure_result(
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
            ),
        )

    merge_status = "merged" if head_ok else "merged_sha_unknown"
    notes: List[str] = []
    if not head_ok:
        notes.append(
            "Merge succeeded but HEAD SHA could not be read after merge; commit hash unknown."
        )
    git_ops = GitOperationsMetadata(
        branch_created=deliver_result.branch_name,
        commits=[GitCommitMetadata(hash=sha, message=commit_msg)],
        merge=GitMergeMetadata(
            target_branch=DEVELOPMENT_BRANCH,
            strategy="merge",
            merge_commit_hash=sha,
            status=merge_status,
        ),
    )
    return DeliveryMergeResult(git_ops=git_ops, notes=notes)
