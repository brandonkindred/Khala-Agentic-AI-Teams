"""Policy-driven Git operations tool agent."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List

from shared.git_utils import _run_git

from .models import GitOperationInput, GitOperationOutput

logger = logging.getLogger(__name__)

_BRANCH_RE = re.compile(r"^(feature|fix|refactor)/[A-Za-z0-9_-]+-[a-z0-9-]+$")


class GitOperationsToolAgent:
    """Execute controlled branch/commit/merge operations for backend workflows."""

    def run(self, input_data: GitOperationInput) -> GitOperationOutput:
        if input_data.requested_operation == "create_branch":
            return self._create_branch(input_data)
        if input_data.requested_operation == "commit_changes":
            return self._commit_changes(input_data)
        if input_data.requested_operation == "merge_to_development":
            return self._merge_to_development(input_data)
        return self._abort_or_reset(input_data)

    @staticmethod
    def _branch_name(input_data: GitOperationInput) -> str:
        branch = input_data.branch.naming_template.format(
            task_id=input_data.task_id,
            slug=input_data.branch.slug,
        )
        return branch.strip()

    @staticmethod
    def _repo(path: str) -> Path:
        repo = Path(path).resolve()
        if not (repo / ".git").exists():
            raise ValueError("Not a git repository")
        return repo

    def _create_branch(self, input_data: GitOperationInput) -> GitOperationOutput:
        out = GitOperationOutput(
            task_id=input_data.task_id,
            operation="create_branch",
            status="failed",
            base_branch=input_data.base_branch,
        )
        try:
            repo = self._repo(input_data.repo_path)
            branch_name = self._branch_name(input_data)
            out.branch_name = branch_name
            if not _BRANCH_RE.match(branch_name):
                out.status = "blocked"
                out.policy_findings.append(f"Invalid branch name policy: {branch_name}")
                return out

            rc, status = _run_git(repo, ["git", "status", "--porcelain"])
            if rc != 0:
                out.notes.append(status)
                return out
            if status.strip():
                out.status = "blocked"
                out.checks["worktree_clean_pre"] = "fail"
                out.policy_findings.append("Working tree must be clean before branch creation")
                return out
            out.checks["worktree_clean_pre"] = "pass"

            _run_git(repo, ["git", "fetch", "--all"])
            rc, msg = _run_git(repo, ["git", "checkout", input_data.base_branch])
            if rc != 0:
                out.notes.append(msg)
                return out
            _run_git(repo, ["git", "pull", "--ff-only"])
            rc, exists = _run_git(repo, ["git", "rev-parse", "--verify", branch_name])
            if rc == 0 and exists.strip():
                out.status = "blocked"
                out.policy_findings.append(f"Branch already exists: {branch_name}")
                return out

            rc, msg = _run_git(repo, ["git", "checkout", "-b", branch_name, input_data.base_branch])
            if rc != 0:
                out.notes.append(msg)
                return out
            out.status = "success"
            out.notes.append(f"Created branch {branch_name}")
            return out
        except Exception as err:
            out.notes.append(str(err))
            return out

    def _commit_changes(self, input_data: GitOperationInput) -> GitOperationOutput:
        out = GitOperationOutput(
            task_id=input_data.task_id,
            operation="commit_changes",
            status="failed",
            base_branch=input_data.base_branch,
            branch_name=self._branch_name(input_data),
        )
        try:
            repo = self._repo(input_data.repo_path)
            rc, changed = _run_git(repo, ["git", "status", "--porcelain"])
            if rc != 0:
                out.notes.append(changed)
                return out
            if not changed.strip():
                out.status = "blocked"
                out.policy_findings.append("No changed files to commit")
                return out

            files = [line[3:] for line in changed.splitlines() if len(line) > 3]
            out.files_committed = files
            allowed = input_data.scope_guard.allowed_paths or []
            out_of_scope = []
            if allowed:
                for f in files:
                    if not any(f == p or f.startswith(p.rstrip("/") + "/") for p in allowed):
                        out_of_scope.append(f)
            if out_of_scope:
                out.status = "blocked"
                out.checks["scope_guard"] = "fail"
                out.policy_findings.append(f"Out-of-scope files changed: {', '.join(out_of_scope)}")
                return out
            out.checks["scope_guard"] = "pass"

            sensitive = [f for f in files if f.endswith(".env") or "secret" in f.lower()]
            if sensitive:
                out.status = "blocked"
                out.policy_findings.append(f"Sensitive files detected: {', '.join(sensitive)}")
                return out

            _run_git(repo, ["git", "add", "-A"])
            message = input_data.commit.message_template.format(task_id=input_data.task_id)
            if not message.strip():
                message = f"feat(backend): complete task [{input_data.task_id}]"

            rc, msg = _run_git(repo, ["git", "commit", "-m", message])
            if rc != 0:
                out.notes.append(msg)
                return out
            rc, commit_hash = _run_git(repo, ["git", "rev-parse", "HEAD"])
            if rc == 0:
                out.commit_hashes = [commit_hash.strip()]
            out.status = "success"
            return out
        except Exception as err:
            out.notes.append(str(err))
            return out

    def _merge_to_development(self, input_data: GitOperationInput) -> GitOperationOutput:
        out = GitOperationOutput(
            task_id=input_data.task_id,
            operation="merge_to_development",
            status="failed",
            base_branch=input_data.base_branch,
            branch_name=self._branch_name(input_data),
        )
        try:
            repo = self._repo(input_data.repo_path)
            token = input_data.merge_token
            if token is None:
                out.status = "blocked"
                out.policy_findings.append("Missing merge approval token")
                return out
            if token.requested_by != "BackendTeamLeadAgent":
                out.status = "blocked"
                out.policy_findings.append("Only BackendTeamLeadAgent may merge to development")
                return out
            if token.branch_name != out.branch_name:
                out.status = "blocked"
                out.policy_findings.append("Merge token branch mismatch")
                return out

            if input_data.merge.require_quality_gates_passed:
                required = ["lint", "static_analysis", "unit_tests", "integration_tests", "security_review", "code_review"]
                missing = [g for g in required if token.quality_gates.get(g) != "pass"]
                if missing:
                    out.status = "blocked"
                    out.checks["quality_gates_required"] = "fail"
                    out.policy_findings.append(f"Quality gates not passed: {', '.join(missing)}")
                    return out
                out.checks["quality_gates_required"] = "pass"

            if input_data.merge.require_clean_worktree:
                rc, status = _run_git(repo, ["git", "status", "--porcelain"])
                if rc != 0 or status.strip():
                    out.status = "blocked"
                    out.checks["worktree_clean_pre"] = "fail"
                    out.policy_findings.append("Working tree must be clean before merge")
                    return out
                out.checks["worktree_clean_pre"] = "pass"

            _run_git(repo, ["git", "fetch", "--all"])
            _run_git(repo, ["git", "checkout", input_data.base_branch])
            _run_git(repo, ["git", "pull", "--ff-only"])
            _run_git(repo, ["git", "checkout", out.branch_name])

            if input_data.merge.rebase_before_merge:
                rc, msg = _run_git(repo, ["git", "rebase", input_data.base_branch])
                if rc != 0:
                    out.status = "blocked"
                    out.checks["rebase_sync"] = "fail"
                    out.policy_findings.append(f"Rebase failed: {msg[:200]}")
                    return out
                out.checks["rebase_sync"] = "pass"

            _run_git(repo, ["git", "checkout", input_data.base_branch])
            if input_data.merge.strategy == "squash":
                rc, msg = _run_git(repo, ["git", "merge", "--squash", out.branch_name])
                if rc != 0:
                    out.status = "blocked"
                    out.policy_findings.append(f"Merge conflict: {msg[:200]}")
                    out.checks["merge_conflicts"] = "conflicts"
                    return out
                commit_msg = f"chore(backend): merge {out.branch_name} [{input_data.task_id}]"
                rc, msg = _run_git(repo, ["git", "commit", "-m", commit_msg])
                if rc != 0:
                    out.notes.append(msg)
                    return out
                out.notes.append("Squash merge used per policy")
            elif input_data.merge.strategy == "no_ff":
                rc, msg = _run_git(repo, ["git", "merge", "--no-ff", out.branch_name, "-m", f"Merge {out.branch_name}"])
                if rc != 0:
                    out.status = "blocked"
                    out.policy_findings.append(f"Merge conflict: {msg[:200]}")
                    out.checks["merge_conflicts"] = "conflicts"
                    return out
            else:
                rc, msg = _run_git(repo, ["git", "merge", "--ff-only", out.branch_name])
                if rc != 0:
                    out.status = "blocked"
                    out.policy_findings.append(f"FF merge failed: {msg[:200]}")
                    out.checks["merge_conflicts"] = "conflicts"
                    return out

            rc, merge_hash = _run_git(repo, ["git", "rev-parse", "HEAD"])
            if rc == 0:
                out.merge_commit_hash = merge_hash.strip()
            out.checks["merge_conflicts"] = "none"
            out.status = "success"
            return out
        except Exception as err:
            out.notes.append(str(err))
            return out

    def _abort_or_reset(self, input_data: GitOperationInput) -> GitOperationOutput:
        out = GitOperationOutput(
            task_id=input_data.task_id,
            operation="abort_or_reset",
            status="success",
            base_branch=input_data.base_branch,
            branch_name=self._branch_name(input_data),
        )
        try:
            repo = self._repo(input_data.repo_path)
            _run_git(repo, ["git", "rebase", "--abort"])
            _run_git(repo, ["git", "merge", "--abort"])
            _run_git(repo, ["git", "checkout", input_data.base_branch])
            out.notes.append("Abort/reset attempted")
        except Exception as err:
            out.status = "failed"
            out.notes.append(str(err))
        return out
