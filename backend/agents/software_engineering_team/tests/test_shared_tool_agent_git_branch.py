"""Branch-coverage tests for the shared git_branch_management tool agent.

The two stacks' concrete modules now re-export
``software_engineering_team.shared.tool_agent_git_branch``; these tests exercise
the ``deliver`` failure/success branches (existing team tests only cover the happy
paths) using a stubbed ``.git`` directory and monkeypatched git helpers, so they
are hermetic and deterministic.
"""

from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from software_engineering_team.shared import tool_agent_git_branch as mod


def _agent():
    return mod.GitBranchManagementToolAgent()


def _inp(**kw):
    base = dict(
        repo_path=None,
        task_id="t1",
        task_title="My Title",
        task_description="do the thing",
        feature_branch_name=None,
        current_files={},
        build_verifier=None,
        build_verify_label="",
        linting_tool_agent=None,
        lint_agent_type="",
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _passing_lint_agent() -> MagicMock:
    agent = MagicMock()
    agent.run.return_value = MagicMock(
        execution_result=MagicMock(success=True), passed=True, linter_issues=[]
    )
    return agent


def _failing_lint_agent() -> MagicMock:
    agent = MagicMock()
    agent.run.return_value = MagicMock(
        execution_result=MagicMock(success=False), passed=False, linter_issues=["boom"]
    )
    return agent


def _git_dir(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


# --- existing-branch path --------------------------------------------------


def test_deliver_existing_branch_success(tmp_path, monkeypatch):
    _git_dir(tmp_path)
    monkeypatch.setattr(mod, "commit_working_tree", lambda *a, **k: (True, ""))
    monkeypatch.setattr(mod, "merge_branch", lambda *a, **k: (True, ""))
    monkeypatch.setattr(mod, "delete_branch", lambda *a, **k: (True, ""))
    monkeypatch.setattr(mod, "checkout_branch", lambda *a, **k: (True, ""))
    out = _agent().deliver(_inp(repo_path=str(tmp_path), feature_branch_name="feature/x"))
    assert out.success is True
    assert "Merged feature/x" in out.summary


def test_deliver_existing_branch_merge_fail(tmp_path, monkeypatch):
    _git_dir(tmp_path)
    monkeypatch.setattr(mod, "commit_working_tree", lambda *a, **k: (True, ""))
    monkeypatch.setattr(mod, "merge_branch", lambda *a, **k: (False, "conflict"))
    monkeypatch.setattr(mod, "abort_merge", lambda *a, **k: (True, ""))
    monkeypatch.setattr(mod, "checkout_branch", lambda *a, **k: (True, ""))
    out = _agent().deliver(_inp(repo_path=str(tmp_path), feature_branch_name="feature/x"))
    assert out.success is False
    assert "Merge failed" in out.summary


def test_deliver_existing_branch_gate_blocks_merge(tmp_path, monkeypatch):
    """Regression: a failing pre-merge quality gate must skip the merge entirely."""
    _git_dir(tmp_path)
    monkeypatch.setattr(mod, "commit_working_tree", lambda *a, **k: (True, ""))
    merge_calls = []
    monkeypatch.setattr(mod, "merge_branch", lambda *a, **k: merge_calls.append(a) or (True, ""))
    checkout_calls = []
    monkeypatch.setattr(
        mod, "checkout_branch", lambda *a, **k: checkout_calls.append(a) or (True, "")
    )
    out = _agent().deliver(
        _inp(
            repo_path=str(tmp_path),
            feature_branch_name="feature/x",
            build_verifier=lambda repo_path, label, task_id: (False, "build broke"),
            build_verify_label="backend",
        )
    )
    assert out.success is False
    assert out.summary == "Pre-merge quality gate failed: Build failed: build broke"
    assert merge_calls == []
    assert checkout_calls[-1][1] == "feature/x"


def test_deliver_existing_branch_gate_passes_autofix_commit_before_merge(tmp_path, monkeypatch):
    """A passing gate sweeps up any autofix commit before the merge proceeds.

    Both operations are recorded into one shared, call-ordered list so the
    assertion actually verifies ordering -- separate per-op lists would pass
    even if an implementation merged before sweeping up the autofix commit.
    """
    _git_dir(tmp_path)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mod,
        "commit_working_tree",
        lambda *a, **k: calls.append(("commit_working_tree", a[1])) or (True, ""),
    )
    monkeypatch.setattr(
        mod,
        "merge_branch",
        lambda *a, **k: calls.append(("merge_branch", "")) or (True, ""),
    )
    monkeypatch.setattr(mod, "delete_branch", lambda *a, **k: (True, ""))
    monkeypatch.setattr(mod, "checkout_branch", lambda *a, **k: (True, ""))
    out = _agent().deliver(
        _inp(
            repo_path=str(tmp_path),
            feature_branch_name="feature/x",
            build_verifier=lambda repo_path, label, task_id: (True, ""),
            linting_tool_agent=_passing_lint_agent(),
            lint_agent_type="backend",
        )
    )
    assert out.success is True
    assert calls == [
        ("commit_working_tree", "chore: finalize before merge"),
        ("commit_working_tree", "chore: pre-merge quality gate autofix"),
        ("merge_branch", ""),
    ]


# --- fallback path (no feature_branch_name) --------------------------------


def test_deliver_fallback_creation_fail(tmp_path, monkeypatch):
    _git_dir(tmp_path)
    monkeypatch.setattr(
        mod.GitBranchManagementToolAgent,
        "create_feature_branch",
        lambda self, *a, **k: (False, None),
    )
    out = _agent().deliver(_inp(repo_path=str(tmp_path)))
    assert out.success is False
    assert "Feature branch creation failed" in out.summary


def test_deliver_fallback_write_fail(tmp_path, monkeypatch):
    _git_dir(tmp_path)
    from software_engineering_team.shared import repo_writer

    monkeypatch.setattr(
        mod.GitBranchManagementToolAgent,
        "create_feature_branch",
        lambda self, *a, **k: (True, "feature/y"),
    )
    monkeypatch.setattr(repo_writer, "write_agent_output", lambda *a, **k: (False, "disk full"))
    monkeypatch.setattr(mod, "checkout_branch", lambda *a, **k: (True, ""))
    out = _agent().deliver(_inp(repo_path=str(tmp_path), current_files={"a.py": "x"}))
    assert out.success is False
    assert "Write failed" in out.summary


def test_deliver_fallback_merge_fail(tmp_path, monkeypatch):
    _git_dir(tmp_path)
    from software_engineering_team.shared import repo_writer

    monkeypatch.setattr(
        mod.GitBranchManagementToolAgent,
        "create_feature_branch",
        lambda self, *a, **k: (True, "feature/y"),
    )
    monkeypatch.setattr(repo_writer, "write_agent_output", lambda *a, **k: (True, ""))
    monkeypatch.setattr(mod, "merge_branch", lambda *a, **k: (False, "conflict"))
    monkeypatch.setattr(mod, "abort_merge", lambda *a, **k: (True, ""))
    monkeypatch.setattr(mod, "checkout_branch", lambda *a, **k: (True, ""))
    out = _agent().deliver(_inp(repo_path=str(tmp_path), current_files={"a.py": "x"}))
    assert out.success is False
    assert "Merge failed" in out.summary


def test_deliver_fallback_gate_blocks_merge(tmp_path, monkeypatch):
    """Regression: a failing pre-merge quality gate must skip the merge on the
    fallback (newly-created-branch) path too, and restore development."""
    _git_dir(tmp_path)
    from software_engineering_team.shared import repo_writer

    monkeypatch.setattr(
        mod.GitBranchManagementToolAgent,
        "create_feature_branch",
        lambda self, *a, **k: (True, "feature/y"),
    )
    monkeypatch.setattr(repo_writer, "write_agent_output", lambda *a, **k: (True, ""))
    merge_calls = []
    monkeypatch.setattr(mod, "merge_branch", lambda *a, **k: merge_calls.append(a) or (True, ""))
    checkout_calls = []
    monkeypatch.setattr(
        mod, "checkout_branch", lambda *a, **k: checkout_calls.append(a) or (True, "")
    )
    out = _agent().deliver(
        _inp(
            repo_path=str(tmp_path),
            current_files={"a.py": "x"},
            linting_tool_agent=_failing_lint_agent(),
            lint_agent_type="backend",
        )
    )
    assert out.success is False
    assert out.summary == "Pre-merge quality gate failed: Lint failed."
    assert merge_calls == []
    assert checkout_calls[-1][1] == mod.DEVELOPMENT_BRANCH


def test_deliver_fallback_success(tmp_path, monkeypatch):
    _git_dir(tmp_path)
    from software_engineering_team.shared import repo_writer

    monkeypatch.setattr(
        mod.GitBranchManagementToolAgent,
        "create_feature_branch",
        lambda self, *a, **k: (True, "feature/y"),
    )
    monkeypatch.setattr(repo_writer, "write_agent_output", lambda *a, **k: (True, ""))
    monkeypatch.setattr(mod, "merge_branch", lambda *a, **k: (True, ""))
    monkeypatch.setattr(mod, "delete_branch", lambda *a, **k: (True, ""))
    monkeypatch.setattr(mod, "checkout_branch", lambda *a, **k: (True, ""))
    out = _agent().deliver(_inp(repo_path=str(tmp_path), current_files={"a.py": "x"}))
    assert out.success is True
    assert "Merged feature/y" in out.summary


# --- create_feature_branch -------------------------------------------------


def test_create_feature_branch_git_helper_fail(tmp_path, monkeypatch):
    _git_dir(tmp_path)
    monkeypatch.setattr(mod, "git_create_feature_branch", lambda *a, **k: (False, ""))
    ok, name = _agent().create_feature_branch(tmp_path, "t1", "Title")
    assert ok is False
    assert name is None


@pytest.mark.parametrize("missing", [True, False])
def test_deliver_non_git(tmp_path, missing):
    repo = None if missing else tmp_path  # tmp_path exists but has no .git
    out = _agent().deliver(_inp(repo_path=str(repo) if repo else ""))
    assert out.success is False
    assert "Not a git repository" in out.summary
