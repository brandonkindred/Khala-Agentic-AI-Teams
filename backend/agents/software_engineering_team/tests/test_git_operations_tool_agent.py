"""Unit tests for GitOperationsToolAgent."""

import subprocess
from pathlib import Path

from git_operations_tool_agent import GitOperationsToolAgent
from git_operations_tool_agent.models import GitOperationInput


def _init_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "development"], cwd=tmp_path, check=True, capture_output=True)


def test_create_branch_and_commit_scope_guard(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    agent = GitOperationsToolAgent()

    create_out = agent.run(
        GitOperationInput(
            task_id="BE-1",
            repo_path=str(tmp_path),
            requested_operation="create_branch",
            requesting_agent="PythonImplementationAgent",
            branch={"naming_template": "feature/{task_id}-{slug}", "slug": "invoice"},
        )
    )
    assert create_out.status == "success"
    assert create_out.branch_name.startswith("feature/BE-1-")

    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "api.py").write_text("print('ok')\n", encoding="utf-8")

    commit_out = agent.run(
        GitOperationInput(
            task_id="BE-1",
            repo_path=str(tmp_path),
            requested_operation="commit_changes",
            requesting_agent="PythonImplementationAgent",
            branch={"naming_template": "feature/{task_id}-{slug}", "slug": "invoice"},
            scope_guard={"allowed_paths": ["src"]},
            commit={"message_template": "feat(billing): add invoice [BE-1]", "include_generated_body": True},
        )
    )
    assert commit_out.status == "success"
    assert len(commit_out.commit_hashes) == 1


def test_merge_requires_token_and_quality_gates(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    agent = GitOperationsToolAgent()

    agent.run(
        GitOperationInput(
            task_id="BE-2",
            repo_path=str(tmp_path),
            requested_operation="create_branch",
            requesting_agent="PythonImplementationAgent",
            branch={"naming_template": "feature/{task_id}-{slug}", "slug": "auth-fix"},
        )
    )
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "service.py").write_text("print('ok')\n", encoding="utf-8")
    agent.run(
        GitOperationInput(
            task_id="BE-2",
            repo_path=str(tmp_path),
            requested_operation="commit_changes",
            requesting_agent="PythonImplementationAgent",
            branch={"naming_template": "feature/{task_id}-{slug}", "slug": "auth-fix"},
            scope_guard={"allowed_paths": ["src"]},
            commit={"message_template": "fix(auth): update service [BE-2]", "include_generated_body": False},
        )
    )

    blocked = agent.run(
        GitOperationInput(
            task_id="BE-2",
            repo_path=str(tmp_path),
            requested_operation="merge_to_development",
            requesting_agent="BackendTeamLeadAgent",
            branch={"naming_template": "feature/{task_id}-{slug}", "slug": "auth-fix"},
        )
    )
    assert blocked.status == "blocked"

    merged = agent.run(
        GitOperationInput(
            task_id="BE-2",
            repo_path=str(tmp_path),
            requested_operation="merge_to_development",
            requesting_agent="BackendTeamLeadAgent",
            branch={"naming_template": "feature/{task_id}-{slug}", "slug": "auth-fix"},
            merge_token={
                "task_id": "BE-2",
                "branch_name": "feature/BE-2-auth-fix",
                "requested_by": "BackendTeamLeadAgent",
                "quality_gates": {
                    "lint": "pass",
                    "static_analysis": "pass",
                    "unit_tests": "pass",
                    "integration_tests": "pass",
                    "security_review": "pass",
                    "code_review": "pass",
                },
                "approvals": {
                    "code_review_agent": "approved",
                    "security_review_agent": "approved",
                },
            },
        )
    )
    assert merged.status == "success"
    assert merged.merge_commit_hash
