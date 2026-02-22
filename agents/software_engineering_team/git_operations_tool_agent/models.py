"""Models for Git Operations Tool Agent."""

from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


RequestedOperation = Literal["create_branch", "commit_changes", "merge_to_development", "abort_or_reset"]
MergeStrategy = Literal["squash", "no_ff", "ff_only"]


class BranchPolicy(BaseModel):
    naming_template: str = "feature/{task_id}-{slug}"
    slug: str = "task"


class CommitPolicy(BaseModel):
    message_template: str = "feat(backend): complete task [{task_id}]"
    include_generated_body: bool = True


class MergePolicy(BaseModel):
    strategy: MergeStrategy = "squash"
    require_clean_worktree: bool = True
    require_quality_gates_passed: bool = True
    rebase_before_merge: bool = True


class ScopeGuard(BaseModel):
    allowed_paths: List[str] = Field(default_factory=list)


class MergeApprovalToken(BaseModel):
    task_id: str
    branch_name: str
    quality_gates: Dict[str, str] = Field(default_factory=dict)
    approvals: Dict[str, str] = Field(default_factory=dict)
    requested_by: str


class GitOperationInput(BaseModel):
    task_id: str
    repo_path: str
    base_branch: str = "development"
    requested_operation: RequestedOperation
    requesting_agent: str
    branch: BranchPolicy = Field(default_factory=BranchPolicy)
    commit: CommitPolicy = Field(default_factory=CommitPolicy)
    merge: MergePolicy = Field(default_factory=MergePolicy)
    scope_guard: ScopeGuard = Field(default_factory=ScopeGuard)
    merge_token: Optional[MergeApprovalToken] = None


class GitOperationOutput(BaseModel):
    task_id: str
    operation: RequestedOperation
    status: Literal["success", "blocked", "failed"]
    branch_name: str = ""
    commit_hashes: List[str] = Field(default_factory=list)
    merge_commit_hash: str = ""
    base_branch: str = "development"
    files_committed: List[str] = Field(default_factory=list)
    checks: Dict[str, str] = Field(default_factory=dict)
    notes: List[str] = Field(default_factory=list)
    policy_findings: List[str] = Field(default_factory=list)
