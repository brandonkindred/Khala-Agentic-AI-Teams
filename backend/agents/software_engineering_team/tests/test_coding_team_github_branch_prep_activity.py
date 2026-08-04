"""Activity-level tests for github_branch_prep_activity (#3989).

The exhaustive recovery-matrix coverage for the underlying
``_prepare_issue_branch`` already lives in ``test_coding_team_branch_prep_recovery.py``
and ``test_coding_team_github_source.py``'s ``TestPrepareIssueBranch``/
``TestGitCredentialThreading``. This file only needs to prove the ACTIVITY
WRAPPER correctly translates the dict-request/dict-response Temporal shape
to/from the underlying tuple-returning function: a real success path, a real
fail-closed path, the wrapper's own request validation, and token/auth-env
threading -- the "equivalent to the current thread-mode behavior" coverage
#3989's acceptance criterion asks for.
"""

from __future__ import annotations

import pathlib
import subprocess
from typing import Any, Optional

import pytest

from software_engineering_team.tests.conftest import (
    _ensure_real_modules,
    _expected_basic_header,
    _stub_orchestrator_only,
)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import ``coding_team_main`` fresh, with the real git/GitHub stack in place."""
    _ensure_real_modules()
    _stub_orchestrator_only(monkeypatch)
    from software_engineering_team.api import coding_team_main as api_main

    return api_main


def _git(repo: str, *args: str) -> None:
    """Run a git command in ``repo`` with ``check=True``.

    Raises ``subprocess.CalledProcessError`` on a non-zero exit.
    """
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True)


def _init_repo(path: pathlib.Path) -> str:
    """Create a temporary git repo under ``path / "repo"`` and return its path.

    Configures user identity, disables signing, forces the branch to "main",
    creates a seed commit, and adds a self-referential "origin" remote so
    fetch works without a real network remote.
    """
    repo = str(path / "repo")
    import os

    os.makedirs(repo, exist_ok=True)
    _git(repo, "init", "-q")
    # Disable commit signing in case the host environment forces it.
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "tag.gpgsign", "false")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    # Force the branch to "main" regardless of the host's init.defaultBranch.
    _git(repo, "checkout", "-q", "-B", "main")
    with open(f"{repo}/README.md", "w") as fh:
        fh.write("seed\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "--no-gpg-sign", "-m", "seed")
    # Self-alias as origin so fetch works without a real remote.
    _git(repo, "remote", "add", "origin", repo)
    return repo


def test_branch_prep_activity_clean_checkout_returns_ok_true(api, tmp_path) -> None:
    """A clean checkout succeeds and the activity returns the ok/notes shape
    the caller (a future #3993 workflow) will branch on, proving real
    delegation to _prepare_issue_branch rather than a stub."""
    from software_engineering_team.temporal.coding_team_github_activities import (
        github_branch_prep_activity,
    )

    repo = _init_repo(tmp_path)
    _git(repo, "fetch", "origin", "main")

    out = github_branch_prep_activity(
        {
            "repo_path": repo,
            "remote": "origin",
            "default_branch": "main",
            "integration_branch": "khala/issue-9",
            "issue_number": 9,
        }
    )

    assert out == {"ok": True, "error": None, "notes": []}
    head = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == "khala/issue-9"


def test_branch_prep_activity_unsafe_ref_returns_ok_false(api) -> None:
    """An unsafe ref is rejected fail-closed before any git operation --
    the activity surfaces this as ok=False with the underlying error message,
    not an exception (a git-level failure, not a caller-wiring bug)."""
    from software_engineering_team.temporal.coding_team_github_activities import (
        github_branch_prep_activity,
    )

    out = github_branch_prep_activity(
        {
            "repo_path": "/nonexistent",
            "remote": "origin",
            "default_branch": "main",
            "integration_branch": "-evil-name",
        }
    )

    assert out["ok"] is False
    assert "unsafe" in (out["error"] or "")
    assert out["notes"] == []


@pytest.mark.parametrize(
    "request_dict,expected_fields,fake_token",
    [
        ({"repo_path": "/x", "remote": "origin"}, ["default_branch", "integration_branch"], None),
        (
            {
                "repo_path": "/x",
                "remote": "origin",
                "default_branch": "main",
                "integration_branch": "",
            },
            ["integration_branch"],
            None,
        ),
        (
            {
                "repo_path": "/x",
                "remote": "origin",
                "default_branch": "main",
                "integration_branch": "",
                "token": "fake-token-xyz",
            },
            ["integration_branch"],
            "fake-token-xyz",
        ),
    ],
)
def test_branch_prep_activity_raises_on_missing_required_field(
    request_dict: dict[str, Any], expected_fields: list[str], fake_token: Optional[str]
) -> None:
    """A missing OR falsy-but-present required field is a caller-wiring bug,
    not a git failure -- it must raise, not be conflated with ok=False, and
    the message must name only the missing fields, never the request payload
    (which may carry a token -- an activity exception message is recorded in
    Temporal history)."""
    from software_engineering_team.temporal.coding_team_github_activities import (
        github_branch_prep_activity,
    )

    with pytest.raises(ValueError, match="missing") as exc_info:
        github_branch_prep_activity(request_dict)

    msg = str(exc_info.value)
    for field in expected_fields:
        assert field in msg
    assert repr(request_dict) not in msg
    if fake_token is not None:
        assert fake_token not in msg


def test_branch_prep_activity_passes_auth_env_to_fetch(api, monkeypatch) -> None:
    """The token must reach both fetch calls (base branch + issue-branch
    continuation candidate) transiently, and never a local-only git op --
    mirrors TestGitCredentialThreading.test_prepare_issue_branch_passes_auth_env_to_fetch
    at the activity boundary."""
    from software_engineering_team.temporal.coding_team_github_activities import (
        github_branch_prep_activity,
    )

    calls = []

    def fake_git(
        repo_path: str, *args: str, timeout: float = 120.0, env: Optional[dict[str, str]] = None
    ) -> tuple[int, str]:
        calls.append((args, env))
        return 0, ""

    monkeypatch.setattr(api, "_working_tree_dirty", lambda p: (True, False, None))
    monkeypatch.setattr(api, "_git", fake_git)

    out = github_branch_prep_activity(
        {
            "repo_path": "/repo",
            "remote": "origin",
            "default_branch": "main",
            "integration_branch": "khala/issue-1",
            "token": "tok-123",
        }
    )

    assert out["ok"] is True, out["error"]
    fetches = [(args, env) for args, env in calls if args[0] == "fetch"]
    assert len(fetches) == 2
    for _args, env in fetches:
        assert env is not None
        assert env["GIT_CONFIG_VALUE_0"] == _expected_basic_header("tok-123")
    assert all(env is None for args, env in calls if args[0] != "fetch")


def test_branch_prep_activity_without_token_uses_no_auth_env(api, monkeypatch) -> None:
    """No token in the request means no auth env on any call."""
    from software_engineering_team.temporal.coding_team_github_activities import (
        github_branch_prep_activity,
    )

    calls = []

    def fake_git(
        repo_path: str, *args: str, timeout: float = 120.0, env: Optional[dict[str, str]] = None
    ) -> tuple[int, str]:
        calls.append((args, env))
        return 0, ""

    monkeypatch.setattr(api, "_working_tree_dirty", lambda p: (True, False, None))
    monkeypatch.setattr(api, "_git", fake_git)

    out = github_branch_prep_activity(
        {
            "repo_path": "/repo",
            "remote": "origin",
            "default_branch": "main",
            "integration_branch": "khala/issue-1",
        }
    )

    assert out["ok"] is True
    assert all(env is None for _, env in calls)


def test_branch_prep_activity_registered_under_expected_temporal_name() -> None:
    """The activity must be registered as ``coding_team_github_branch_prep``,
    matching the name workflow.execute_activity dispatch (and any future
    #3993 workflow wiring) will reference -- a decorator with a wrong or
    accidentally-dropped name would silently break that dispatch without
    this test catching it."""
    from software_engineering_team.temporal.coding_team_github_activities import (
        github_branch_prep_activity,
    )

    definition = github_branch_prep_activity.__temporal_activity_definition
    assert definition.name == "coding_team_github_branch_prep"
