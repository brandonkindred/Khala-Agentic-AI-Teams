"""Tests for ambient git commit identity (git_identity_env).

GitHub-cloned checkouts have no repo-local user.name/user.email and the agent
containers set no global git config, so a bare `git commit` fails with
"Author identity unknown". git_identity_env() must make identity ambient for
every command routed through _run_git, configurable via
GIT_COMMIT_USER_NAME / GIT_COMMIT_USER_EMAIL.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from shared.git.git_utils import (
    commit_working_tree,
    git_identity_env,
    initialize_new_repo,
)


@pytest.fixture
def identity_free_env(monkeypatch, tmp_path):
    """Reproduce the agent container: no git identity configured anywhere."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig-missing"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    for var in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_COMMIT_USER_NAME",
        "GIT_COMMIT_USER_EMAIL",
        "EMAIL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_identity_env_defaults(identity_free_env):
    env = git_identity_env()
    assert env["GIT_AUTHOR_NAME"] == "Khala"
    assert env["GIT_AUTHOR_EMAIL"] == "brandon.kindred@gmail.com"
    assert env["GIT_COMMITTER_NAME"] == "Khala"
    assert env["GIT_COMMITTER_EMAIL"] == "brandon.kindred@gmail.com"


def test_identity_env_respects_overrides(identity_free_env, monkeypatch):
    monkeypatch.setenv("GIT_COMMIT_USER_NAME", "Custom Bot")
    monkeypatch.setenv("GIT_COMMIT_USER_EMAIL", "bot@example.com")
    env = git_identity_env()
    assert env["GIT_AUTHOR_NAME"] == "Custom Bot"
    assert env["GIT_AUTHOR_EMAIL"] == "bot@example.com"


def test_identity_env_blank_overrides_fall_back(identity_free_env, monkeypatch):
    monkeypatch.setenv("GIT_COMMIT_USER_NAME", "   ")
    monkeypatch.setenv("GIT_COMMIT_USER_EMAIL", "")
    env = git_identity_env()
    assert env["GIT_AUTHOR_NAME"] == "Khala"
    assert env["GIT_AUTHOR_EMAIL"] == "brandon.kindred@gmail.com"


def test_identity_env_never_clobbers_native_git_vars(identity_free_env, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Operator")
    env = git_identity_env()
    assert env["GIT_AUTHOR_NAME"] == "Operator"
    # Gaps are still filled.
    assert env["GIT_AUTHOR_EMAIL"] == "brandon.kindred@gmail.com"


def test_identity_env_preserves_parent_environment(identity_free_env):
    assert "PATH" in git_identity_env()


def test_commit_working_tree_without_any_identity(identity_free_env, tmp_path):
    """Reproduces the container failure: "Author identity unknown"."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")

    ok, msg = commit_working_tree(str(repo), "test commit")

    assert ok is True, msg
    out = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>|%cn <%ce>"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == "Khala <brandon.kindred@gmail.com>|Khala <brandon.kindred@gmail.com>"


def test_initialize_new_repo_writes_configured_identity(identity_free_env, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_COMMIT_USER_NAME", "Custom Bot")
    monkeypatch.setenv("GIT_COMMIT_USER_EMAIL", "bot@example.com")
    repo = tmp_path / "repo"
    ok, msg = initialize_new_repo(str(repo))
    assert ok is True, msg
    name = subprocess.run(
        ["git", "config", "--local", "user.name"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    email = subprocess.run(
        ["git", "config", "--local", "user.email"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    assert (name, email) == ("Custom Bot", "bot@example.com")


def test_identity_env_replaces_blank_native_vars(identity_free_env, monkeypatch):
    """A deployment exporting GIT_AUTHOR_NAME="" must not survive into the
    subprocess env — git rejects empty idents ("fatal: empty ident name")."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "   ")
    env = git_identity_env()
    assert env["GIT_AUTHOR_NAME"] == "Khala"
    assert env["GIT_COMMITTER_EMAIL"] == "brandon.kindred@gmail.com"


def test_commit_succeeds_with_blank_identity_exports(identity_free_env, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    ok, msg = commit_working_tree(str(repo), "test commit")
    assert ok is True, msg
