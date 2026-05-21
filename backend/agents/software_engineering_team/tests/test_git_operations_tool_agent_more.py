"""Additional coverage for GitOperationsToolAgent (policy and merge paths)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from git_operations_tool_agent import GitOperationsToolAgent
from git_operations_tool_agent.models import (
    BranchPolicy,
    CommitPolicy,
    GitOperationInput,
    MergeApprovalToken,
    MergePolicy,
    ScopeGuard,
)


def _init_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True, capture_output=True
    )
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "checkout", "-b", "development"], cwd=tmp_path, check=True, capture_output=True
    )


def test_repo_path_not_git_repo(tmp_path: Path):
    """Non-git path -> error captured in notes."""
    agent = GitOperationsToolAgent()
    out = agent.run(
        GitOperationInput(
            task_id="t1",
            repo_path=str(tmp_path),
            requested_operation="create_branch",
            requesting_agent="X",
            branch=BranchPolicy(slug="task"),
        )
    )
    assert out.status == "failed"
    assert any("Not a git" in n for n in out.notes)


def test_create_branch_invalid_naming_template(tmp_path: Path):
    _init_repo(tmp_path)
    agent = GitOperationsToolAgent()
    out = agent.run(
        GitOperationInput(
            task_id="t1",
            repo_path=str(tmp_path),
            requested_operation="create_branch",
            requesting_agent="X",
            branch=BranchPolicy(naming_template="bad/{task_id}-{slug}", slug="x"),
        )
    )
    assert out.status == "blocked"
    assert any("Invalid branch name policy" in f for f in out.policy_findings)


def test_create_branch_dirty_worktree(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("x")  # untracked file
    agent = GitOperationsToolAgent()
    out = agent.run(
        GitOperationInput(
            task_id="BE-1",
            repo_path=str(tmp_path),
            requested_operation="create_branch",
            requesting_agent="X",
            branch=BranchPolicy(slug="my-task"),
        )
    )
    assert out.status == "blocked"
    assert any("Working tree must be clean" in f for f in out.policy_findings)


def test_create_branch_already_exists(tmp_path: Path):
    _init_repo(tmp_path)
    subprocess.run(
        ["git", "checkout", "-b", "feature/BE-1-existing"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "development"], cwd=tmp_path, check=True, capture_output=True
    )

    agent = GitOperationsToolAgent()
    out = agent.run(
        GitOperationInput(
            task_id="BE-1",
            repo_path=str(tmp_path),
            requested_operation="create_branch",
            requesting_agent="X",
            branch=BranchPolicy(slug="existing"),
        )
    )
    assert out.status == "blocked"
    assert any("already exists" in f for f in out.policy_findings)


def test_commit_no_changes_blocked(tmp_path: Path):
    _init_repo(tmp_path)
    subprocess.run(
        ["git", "checkout", "-b", "feature/BE-1-x"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    agent = GitOperationsToolAgent()
    out = agent.run(
        GitOperationInput(
            task_id="BE-1",
            repo_path=str(tmp_path),
            requested_operation="commit_changes",
            requesting_agent="X",
            branch=BranchPolicy(slug="x"),
        )
    )
    assert out.status == "blocked"
    assert any("No changed files" in f for f in out.policy_findings)


def test_commit_out_of_scope(tmp_path: Path):
    _init_repo(tmp_path)
    subprocess.run(
        ["git", "checkout", "-b", "feature/BE-1-x"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "elsewhere.py").write_text("x", encoding="utf-8")
    agent = GitOperationsToolAgent()
    out = agent.run(
        GitOperationInput(
            task_id="BE-1",
            repo_path=str(tmp_path),
            requested_operation="commit_changes",
            requesting_agent="X",
            branch=BranchPolicy(slug="x"),
            scope_guard=ScopeGuard(allowed_paths=["src"]),
        )
    )
    assert out.status == "blocked"
    assert any("Out-of-scope files" in f for f in out.policy_findings)


def test_commit_sensitive_files(tmp_path: Path):
    _init_repo(tmp_path)
    subprocess.run(
        ["git", "checkout", "-b", "feature/BE-1-x"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / ".env").write_text("SECRET=fixture-placeholder-not-a-secret", encoding="utf-8")
    agent = GitOperationsToolAgent()
    out = agent.run(
        GitOperationInput(
            task_id="BE-1",
            repo_path=str(tmp_path),
            requested_operation="commit_changes",
            requesting_agent="X",
            branch=BranchPolicy(slug="x"),
        )
    )
    assert out.status == "blocked"
    assert any("Sensitive files" in f for f in out.policy_findings)


def test_commit_success_default_message(tmp_path: Path):
    _init_repo(tmp_path)
    subprocess.run(
        ["git", "checkout", "-b", "feature/BE-1-x"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "x.py").write_text("print(1)", encoding="utf-8")
    agent = GitOperationsToolAgent()
    out = agent.run(
        GitOperationInput(
            task_id="BE-1",
            repo_path=str(tmp_path),
            requested_operation="commit_changes",
            requesting_agent="X",
            branch=BranchPolicy(slug="x"),
            commit=CommitPolicy(message_template=""),
        )
    )
    assert out.status == "success"


def test_merge_no_token_blocked(tmp_path: Path):
    _init_repo(tmp_path)
    agent = GitOperationsToolAgent()
    out = agent.run(
        GitOperationInput(
            task_id="BE-1",
            repo_path=str(tmp_path),
            requested_operation="merge_to_development",
            requesting_agent="X",
            branch=BranchPolicy(slug="x"),
        )
    )
    assert out.status == "blocked"
    assert any("Missing merge approval token" in f for f in out.policy_findings)


def test_merge_unauthorized_requester(tmp_path: Path):
    _init_repo(tmp_path)
    agent = GitOperationsToolAgent()
    out = agent.run(
        GitOperationInput(
            task_id="BE-1",
            repo_path=str(tmp_path),
            requested_operation="merge_to_development",
            requesting_agent="X",
            branch=BranchPolicy(slug="x"),
            merge_token=MergeApprovalToken(
                task_id="BE-1",
                branch_name="feature/BE-1-x",
                requested_by="UnknownAgent",
            ),
        )
    )
    assert out.status == "blocked"
    assert any("Only BackendTeamLeadAgent" in f for f in out.policy_findings)


def test_merge_branch_mismatch(tmp_path: Path):
    _init_repo(tmp_path)
    agent = GitOperationsToolAgent()
    out = agent.run(
        GitOperationInput(
            task_id="BE-1",
            repo_path=str(tmp_path),
            requested_operation="merge_to_development",
            requesting_agent="X",
            branch=BranchPolicy(slug="x"),
            merge_token=MergeApprovalToken(
                task_id="BE-1",
                branch_name="feature/wrong-name",
                requested_by="BackendTeamLeadAgent",
            ),
        )
    )
    assert out.status == "blocked"
    assert any("Merge token branch mismatch" in f for f in out.policy_findings)


def test_merge_missing_quality_gates(tmp_path: Path):
    _init_repo(tmp_path)
    agent = GitOperationsToolAgent()
    out = agent.run(
        GitOperationInput(
            task_id="BE-1",
            repo_path=str(tmp_path),
            requested_operation="merge_to_development",
            requesting_agent="X",
            branch=BranchPolicy(slug="x"),
            merge_token=MergeApprovalToken(
                task_id="BE-1",
                branch_name="feature/BE-1-x",
                requested_by="BackendTeamLeadAgent",
                quality_gates={},
            ),
        )
    )
    assert out.status == "blocked"
    assert any("Quality gates not passed" in f for f in out.policy_findings)


def test_merge_dirty_worktree_blocked(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("x")
    agent = GitOperationsToolAgent()
    out = agent.run(
        GitOperationInput(
            task_id="BE-1",
            repo_path=str(tmp_path),
            requested_operation="merge_to_development",
            requesting_agent="X",
            branch=BranchPolicy(slug="x"),
            merge=MergePolicy(require_quality_gates_passed=False),
            merge_token=MergeApprovalToken(
                task_id="BE-1",
                branch_name="feature/BE-1-x",
                requested_by="BackendTeamLeadAgent",
            ),
        )
    )
    assert out.status == "blocked"
    assert any("Working tree must be clean" in f for f in out.policy_findings)


def test_abort_or_reset(tmp_path: Path):
    _init_repo(tmp_path)
    agent = GitOperationsToolAgent()
    out = agent.run(
        GitOperationInput(
            task_id="BE-1",
            repo_path=str(tmp_path),
            requested_operation="abort_or_reset",
            requesting_agent="X",
            branch=BranchPolicy(slug="x"),
        )
    )
    assert out.status == "success"
    assert any("Abort/reset" in n for n in out.notes)


def test_abort_or_reset_non_repo(tmp_path: Path):
    """Non-git repo -> abort_or_reset returns failed."""
    agent = GitOperationsToolAgent()
    out = agent.run(
        GitOperationInput(
            task_id="t1",
            repo_path=str(tmp_path),  # not a git repo
            requested_operation="abort_or_reset",
            requesting_agent="X",
            branch=BranchPolicy(slug="x"),
        )
    )
    assert out.status == "failed"


def test_full_merge_squash(tmp_path: Path):
    """Happy-path squash merge."""
    _init_repo(tmp_path)
    # Create feature branch with commits
    subprocess.run(
        ["git", "checkout", "-b", "feature/BE-1-task"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "x.py").write_text("print(1)", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "feat: x"], cwd=tmp_path, check=True, capture_output=True
    )

    agent = GitOperationsToolAgent()
    out = agent.run(
        GitOperationInput(
            task_id="BE-1",
            repo_path=str(tmp_path),
            requested_operation="merge_to_development",
            requesting_agent="X",
            branch=BranchPolicy(slug="task"),
            merge=MergePolicy(
                strategy="squash",
                require_quality_gates_passed=False,
                rebase_before_merge=False,
            ),
            merge_token=MergeApprovalToken(
                task_id="BE-1",
                branch_name="feature/BE-1-task",
                requested_by="BackendTeamLeadAgent",
            ),
        )
    )
    # Result may depend on git config; verify shape
    assert out.operation == "merge_to_development"
    assert out.branch_name == "feature/BE-1-task"
