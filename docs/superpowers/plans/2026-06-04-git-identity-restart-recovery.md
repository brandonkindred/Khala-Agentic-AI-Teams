# Git Identity & Restart Recovery/Continuation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every platform git commit a configurable author identity, and make coding-team branch prep recover a wedged checkout and continue an issue's prior progress per `docs/superpowers/specs/2026-06-04-git-identity-and-restart-recovery-design.md`.

**Architecture:** Part 1 adds an ambient-identity environment shim at `_run_git()`, the single subprocess choke point in `software_engineering_team/shared/git_utils.py`. Part 2 rewrites `_prepare_issue_branch()` in `coding_team/api/main.py` around a repo-local `khala.active-issue` git-config marker: dirty trees are committed in place (same issue) or preserved on `khala/rescue/*` branches (foreign/unknown), branches are seeded from the best prior-progress tip, and an orphan-prevention invariant guarantees no reset makes commits unreachable.

**Tech Stack:** Python 3.10+, pytest (real on-disk git repos, no git mocks), ruff. All commands run from `backend/` using `.venv/bin/python`.

**Conventions that apply to every task:** DbC docstrings (`Preconditions:`/`Postconditions:`), no GitHub issue references in code or commit messages, ruff line-length 120.

---

### Task 0: Feature branch + design docs commit

**Files:**
- Commit: `docs/superpowers/specs/2026-06-04-git-identity-and-restart-recovery-design.md`
- Commit: `docs/superpowers/plans/2026-06-04-git-identity-restart-recovery.md`

Note: the working tree also contains unrelated uncommitted changes (GitHub Basic-auth fix, strands tool-registration fix). Never `git add -A`; stage exact paths only.

- [ ] **Step 1: Create the feature branch**

```bash
git -C /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams checkout -b feature/git-identity-restart-recovery
```

- [ ] **Step 2: Commit the spec and plan**

```bash
git -C /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams add docs/superpowers/specs/2026-06-04-git-identity-and-restart-recovery-design.md docs/superpowers/plans/2026-06-04-git-identity-restart-recovery.md
git -C /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams commit -m "docs: design spec and plan for git identity + coding-team restart recovery"
```

---

### Task 1: Ambient git commit identity (Part 1)

**Files:**
- Modify: `backend/agents/software_engineering_team/shared/git_utils.py` (imports; new `_configured_commit_identity()` + `git_identity_env()`; `_run_git()` env; `initialize_new_repo()` identity lines ~327-331)
- Test: `backend/agents/software_engineering_team/tests/test_git_identity.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `backend/agents/software_engineering_team/tests/test_git_identity.py`:

```python
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

from software_engineering_team.shared.git_utils import (
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest agents/software_engineering_team/tests/test_git_identity.py -q`
Expected: ImportError (`git_identity_env` not defined) — then, once stubs exist, `test_commit_working_tree_without_any_identity` FAILS with `ok is False` / "Author identity unknown" and `test_initialize_new_repo_writes_configured_identity` FAILS with `('Khala Agent', 'agent@khala.local')`.

- [ ] **Step 3: Implement in `git_utils.py`**

Add `import os` to the imports block. Add after the `MAIN_BRANCH` constant:

```python
DEFAULT_COMMIT_USER_NAME = "Khala"
DEFAULT_COMMIT_USER_EMAIL = "brandon.kindred@gmail.com"


def _configured_commit_identity() -> Tuple[str, str]:
    """Resolve the configured platform commit identity.

    Postconditions:
        - Returns (name, email), both non-empty; blank or unset
          GIT_COMMIT_USER_NAME / GIT_COMMIT_USER_EMAIL fall back to the
          platform defaults.
    """
    name = (os.environ.get("GIT_COMMIT_USER_NAME") or "").strip() or DEFAULT_COMMIT_USER_NAME
    email = (os.environ.get("GIT_COMMIT_USER_EMAIL") or "").strip() or DEFAULT_COMMIT_USER_EMAIL
    return name, email


def git_identity_env() -> Dict[str, str]:
    """Process environment for git subprocesses with a complete commit identity.

    GitHub-cloned checkouts have no repo-local user.name/user.email and the
    agent containers set no global git config, so a bare `git commit` fails
    with "Author identity unknown". Filling git's native identity variables
    here makes identity ambient for every command routed through _run_git
    without persisting anything into the checkout.

    Preconditions: none.
    Postconditions:
        - Returned dict contains all parent environment entries.
        - GIT_AUTHOR_NAME/EMAIL and GIT_COMMITTER_NAME/EMAIL are present and
          non-empty; values already exported by the operator are unchanged.
    """
    name, email = _configured_commit_identity()
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", name)
    env.setdefault("GIT_AUTHOR_EMAIL", email)
    env.setdefault("GIT_COMMITTER_NAME", name)
    env.setdefault("GIT_COMMITTER_EMAIL", email)
    return env
```

In `_run_git()`, add the env to the subprocess call:

```python
        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=git_identity_env(),
        )
```

In `initialize_new_repo()`, replace the two hardcoded config lines (after the gpgsign line):

```python
    # Set a default local identity so commits work even when no global git config is set
    # (e.g. in CI environments). Local config is repo-scoped and does not affect global settings.
    name, email = _configured_commit_identity()
    _run_git(path, ["git", "config", "user.email", email])
    _run_git(path, ["git", "config", "user.name", name])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest agents/software_engineering_team/tests/test_git_identity.py -q`
Expected: 7 passed.

- [ ] **Step 5: Run the SE git suites + lint, then commit**

Run: `.venv/bin/python -m pytest agents/software_engineering_team/tests/test_git_utils.py agents/software_engineering_team/tests/test_git_utils_more.py agents/software_engineering_team/tests/test_git_utils_extra.py agents/software_engineering_team/tests/test_git_identity.py -q`
Expected: all pass (pre-existing failures, if any, must also fail on a stashed tree before proceeding).

```bash
.venv/bin/python -m ruff check agents/software_engineering_team/shared/git_utils.py agents/software_engineering_team/tests/test_git_identity.py
.venv/bin/python -m ruff format agents/software_engineering_team/shared/git_utils.py agents/software_engineering_team/tests/test_git_identity.py
git -C /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams add backend/agents/software_engineering_team/shared/git_utils.py backend/agents/software_engineering_team/tests/test_git_identity.py
git -C /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams commit -m "feat: ambient configurable git commit identity for all platform git ops"
```

---

### Task 2: Marker + git-graph helpers in coding_team (Part 2 plumbing)

**Files:**
- Modify: `backend/agents/coding_team/api/main.py` (imports; new helpers after `_git()`)
- Test: `backend/agents/coding_team/tests/test_branch_prep_recovery.py` (new)

- [ ] **Step 1: Write the failing tests (new file, real repos)**

Create `backend/agents/coding_team/tests/test_branch_prep_recovery.py`:

```python
"""
Restart recovery & continuation tests for coding-team branch prep.

Every test runs against real on-disk git repositories in an identity-free
environment (no global git config, no identity env vars) so the suite
reproduces the agent container regardless of the developer's machine. The
"remote" is a sibling local repo added as `origin`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

import pytest


def _ensure_real_modules() -> None:
    """Evict synthetic module stubs other test files may have installed.

    test_github_source._stub_heavy_modules() registers a fake
    software_engineering_team.shared.git_utils in sys.modules with no
    __file__; these tests need the real implementation (and an api.main
    bound to it), under any test-execution order.
    """
    stale = False
    for name in (
        "software_engineering_team.shared.git_utils",
        "software_engineering_team.shared",
        "software_engineering_team",
        "coding_team.orchestrator",
    ):
        mod = sys.modules.get(name)
        if mod is not None and not getattr(mod, "__file__", None):
            del sys.modules[name]
            stale = True
    if stale:
        sys.modules.pop("coding_team.api.main", None)
        sys.modules.pop("coding_team.api", None)


def _stub_orchestrator_only() -> None:
    """Keep api.main importable without the heavy agent stack."""
    import types

    if "coding_team.orchestrator" not in sys.modules:
        stub = types.ModuleType("coding_team.orchestrator")
        stub.run_coding_team_orchestrator = lambda *a, **kw: None  # type: ignore[attr-defined]
        sys.modules["coding_team.orchestrator"] = stub


@pytest.fixture
def api():
    _ensure_real_modules()
    _stub_orchestrator_only()
    from coding_team.api import main as api_main

    return api_main


@pytest.fixture
def identity_free_env(monkeypatch, tmp_path):
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


def _run(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)


def _must(cwd: str, *args: str) -> str:
    r = _run(cwd, *args)
    assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr or r.stdout}"
    return r.stdout.strip()


def _identity_env() -> dict:
    """Identity for *test fixture* commits that simulate an interrupted run."""
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.com",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.com",
    }


def _commit_file(repo: str, relpath: str, content: str, message: str) -> None:
    path = os.path.join(repo, relpath)
    os.makedirs(os.path.dirname(path) or repo, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    _must(repo, "add", "-A")
    r = subprocess.run(
        ["git", "-C", repo, "commit", "-q", "--no-gpg-sign", "-m", message],
        capture_output=True,
        text=True,
        env=_identity_env(),
    )
    assert r.returncode == 0, r.stderr


@pytest.fixture
def repo_pair(tmp_path, identity_free_env):
    """(remote_path, clone_path): remote has one commit on main; clone tracks it."""
    remote = str(tmp_path / "remote")
    os.makedirs(remote)
    _must(remote, "init", "-q")
    _must(remote, "config", "commit.gpgsign", "false")
    _must(remote, "checkout", "-q", "-b", "main")
    _commit_file(remote, "README.md", "base\n", "base")
    clone = str(tmp_path / "clone")
    r = subprocess.run(["git", "clone", "-q", remote, clone], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    _must(clone, "config", "commit.gpgsign", "false")
    return remote, clone


def _branch_exists(repo: str, pattern: str) -> Optional[str]:
    out = _must(repo, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{pattern}")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return lines[-1] if lines else None


# ---------------------------------------------------------------------------
# Marker helpers
# ---------------------------------------------------------------------------


class TestActiveIssueMarker:
    def test_read_returns_none_when_unset(self, api, repo_pair) -> None:
        _, clone = repo_pair
        assert api._read_active_issue(clone) is None

    def test_write_then_read_roundtrip(self, api, repo_pair) -> None:
        _, clone = repo_pair
        api._write_active_issue(clone, 7)
        assert api._read_active_issue(clone) == 7
        assert _must(clone, "config", "--local", "khala.active-issue") == "7"

    def test_clear_removes_marker(self, api, repo_pair) -> None:
        _, clone = repo_pair
        api._write_active_issue(clone, 7)
        api._clear_active_issue(clone)
        assert api._read_active_issue(clone) is None

    def test_clear_is_idempotent(self, api, repo_pair) -> None:
        _, clone = repo_pair
        api._clear_active_issue(clone)  # never set; must not raise
        assert api._read_active_issue(clone) is None

    def test_read_garbage_value_returns_none(self, api, repo_pair) -> None:
        _, clone = repo_pair
        _must(clone, "config", "--local", "khala.active-issue", "not-a-number")
        assert api._read_active_issue(clone) is None


# ---------------------------------------------------------------------------
# Git-graph helpers
# ---------------------------------------------------------------------------


class TestGraphHelpers:
    def test_is_ahead_false_for_missing_ref(self, api, repo_pair) -> None:
        _, clone = repo_pair
        assert api._is_ahead(clone, "no-such-branch", "origin/main") is False

    def test_is_ahead_false_when_equal(self, api, repo_pair) -> None:
        _, clone = repo_pair
        assert api._is_ahead(clone, "main", "origin/main") is False

    def test_is_ahead_true_with_extra_commit(self, api, repo_pair) -> None:
        _, clone = repo_pair
        _must(clone, "checkout", "-q", "-b", "khala/issue-7")
        _commit_file(clone, "work.py", "x = 1\n", "progress")
        assert api._is_ahead(clone, "khala/issue-7", "origin/main") is True

    def test_rescue_branch_name_tags_issue_and_avoids_collisions(self, api, repo_pair, monkeypatch) -> None:
        _, clone = repo_pair
        monkeypatch.setattr(api, "_utc_timestamp", lambda: "20260101-000000")
        first = api._rescue_branch_name(clone, 5)
        assert first == "khala/rescue/issue-5-20260101-000000"
        _must(clone, "branch", first)
        second = api._rescue_branch_name(clone, 5)
        assert second == "khala/rescue/issue-5-20260101-000000-1"
        untagged = api._rescue_branch_name(clone, None)
        assert untagged == "khala/rescue/20260101-000000"

    def test_latest_issue_rescue_ref_picks_newest(self, api, repo_pair) -> None:
        _, clone = repo_pair
        _must(clone, "branch", "khala/rescue/issue-7-20260101-000000")
        _must(clone, "branch", "khala/rescue/issue-7-20260102-000000")
        _must(clone, "branch", "khala/rescue/issue-9-20260103-000000")
        assert api._latest_issue_rescue_ref(clone, 7) == "khala/rescue/issue-7-20260102-000000"
        assert api._latest_issue_rescue_ref(clone, 4) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest agents/coding_team/tests/test_branch_prep_recovery.py -q`
Expected: AttributeError — `_read_active_issue` (etc.) not defined on the api module.

- [ ] **Step 3: Implement the helpers in `coding_team/api/main.py`**

Add to imports: `from datetime import datetime, timezone` and extend the existing git_utils import to
`from software_engineering_team.shared.git_utils import DEVELOPMENT_BRANCH, commit_working_tree  # noqa: E402`.

Insert after the `_git()` function:

```python
RESCUE_BRANCH_PREFIX = "khala/rescue/"
ACTIVE_ISSUE_CONFIG_KEY = "khala.active-issue"


def _utc_timestamp() -> str:
    """Wall-clock UTC stamp used in rescue branch names (patchable in tests)."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _read_active_issue(repo_path: str) -> Optional[int]:
    """Read the repo-local active-issue marker.

    The marker means: a job for that issue was mid-flight on this checkout
    and terminated abnormally (restart, kill, delete). It is the only state
    that survives job deletion, so leftover work is attributed through it.

    Postconditions:
        - Returns the issue number, or None when the marker is absent or
          unparseable (treated as unattributed).
    """
    rc, msg = _git(repo_path, "config", "--local", "--get", ACTIVE_ISSUE_CONFIG_KEY)
    if rc != 0:
        return None
    try:
        return int(msg.strip())
    except ValueError:
        return None


def _write_active_issue(repo_path: str, issue_number: int) -> None:
    """Record that a job for issue_number is mid-flight on this checkout."""
    _git(repo_path, "config", "--local", ACTIVE_ISSUE_CONFIG_KEY, str(issue_number))


def _clear_active_issue(repo_path: str) -> None:
    """Remove the marker; idempotent (unsetting a missing key is a no-op)."""
    _git(repo_path, "config", "--local", "--unset", ACTIVE_ISSUE_CONFIG_KEY)


def _is_ahead(repo_path: str, ref: str, base_ref: str) -> bool:
    """True if ref resolves to a commit and has commits not reachable from base_ref."""
    rc, _ = _git(repo_path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if rc != 0:
        return False
    rc, out = _git(repo_path, "rev-list", "--count", f"{base_ref}..{ref}")
    if rc != 0:
        return False
    try:
        return int(out.strip()) > 0
    except ValueError:
        return False


def _reachable_from(repo_path: str, tip: str, container: str) -> bool:
    """True if tip is an ancestor of container (resetting container keeps tip reachable)."""
    rc, _ = _git(repo_path, "merge-base", "--is-ancestor", tip, container)
    return rc == 0


def _rescue_branch_name(repo_path: str, issue: Optional[int]) -> Optional[str]:
    """Allocate an unused rescue branch name.

    Postconditions:
        - Returns `khala/rescue/issue-<issue>-<ts>` (issue known) or
          `khala/rescue/<ts>`, suffixed `-1`..`-9` on collision; None when
          all ten candidates exist.
    """
    tag = f"issue-{issue}-" if issue is not None else ""
    base = f"{RESCUE_BRANCH_PREFIX}{tag}{_utc_timestamp()}"
    for cand in [base] + [f"{base}-{i}" for i in range(1, 10)]:
        rc, _ = _git(repo_path, "rev-parse", "--verify", "--quiet", f"refs/heads/{cand}")
        if rc != 0:
            return cand
    return None


def _latest_issue_rescue_ref(repo_path: str, issue_number: int) -> Optional[str]:
    """Newest rescue ref for the issue (timestamps sort lexicographically)."""
    rc, out = _git(
        repo_path,
        "for-each-ref",
        "--sort=-refname",
        "--count=1",
        "--format=%(refname:short)",
        f"refs/heads/{RESCUE_BRANCH_PREFIX}issue-{issue_number}-*",
    )
    if rc != 0 or not out.strip():
        return None
    return out.strip().splitlines()[0]
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest agents/coding_team/tests/test_branch_prep_recovery.py -q`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git -C /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams add backend/agents/coding_team/api/main.py backend/agents/coding_team/tests/test_branch_prep_recovery.py
git -C /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams commit -m "feat: active-issue marker and git-graph helpers for coding-team recovery"
```

---

### Task 3: Status-aware dirty check + dirty-tree recovery

**Files:**
- Modify: `backend/agents/coding_team/api/main.py` (`_working_tree_dirty()` → 3-tuple; new `_recover_dirty_tree()`)
- Modify: `backend/agents/coding_team/tests/test_github_source.py` (two `_working_tree_dirty` monkeypatch lambdas)
- Test: `backend/agents/coding_team/tests/test_branch_prep_recovery.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `test_branch_prep_recovery.py`:

```python
# ---------------------------------------------------------------------------
# Dirty-tree recovery
# ---------------------------------------------------------------------------


class TestRecoverDirtyTree:
    def test_same_issue_commits_in_place(self, api, repo_pair) -> None:
        _, clone = repo_pair
        _must(clone, "checkout", "-q", "-b", "development")
        with open(os.path.join(clone, "wip.txt"), "w", encoding="utf-8") as fh:
            fh.write("in progress\n")
        tip, note, err = api._recover_dirty_tree(clone, 7, 7, "?? wip.txt")
        assert err is None
        assert tip == "development"
        assert "development" in note
        ok, _, listing = api._working_tree_dirty(clone)
        assert ok is True and listing is None
        assert "wip.txt" in _must(clone, "show", "--stat", "--name-only", "development")

    def test_foreign_issue_rescued_to_tagged_branch(self, api, repo_pair) -> None:
        _, clone = repo_pair
        with open(os.path.join(clone, "wip.txt"), "w", encoding="utf-8") as fh:
            fh.write("other issue\n")
        tip, note, err = api._recover_dirty_tree(clone, 5, 7, "?? wip.txt")
        assert err is None
        assert tip is None  # foreign work is preserved, not a continuation seed
        rescued = _branch_exists(clone, "khala/rescue/issue-5-*")
        assert rescued is not None and rescued in note
        assert "wip.txt" in _must(clone, "show", "--stat", "--name-only", rescued)

    def test_no_marker_rescued_to_untagged_branch(self, api, repo_pair) -> None:
        _, clone = repo_pair
        with open(os.path.join(clone, "wip.txt"), "w", encoding="utf-8") as fh:
            fh.write("who knows\n")
        tip, note, err = api._recover_dirty_tree(clone, None, 7, "?? wip.txt")
        assert err is None and tip is None
        rescued = _branch_exists(clone, "khala/rescue/2*")
        assert rescued is not None
        assert "issue-" not in rescued

    def test_commit_failure_reported(self, api, repo_pair, monkeypatch) -> None:
        _, clone = repo_pair
        with open(os.path.join(clone, "wip.txt"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        monkeypatch.setattr(api, "commit_working_tree", lambda *_a, **_kw: (False, "boom"))
        tip, note, err = api._recover_dirty_tree(clone, None, 7, "?? wip.txt")
        assert tip is None and note is None
        assert "boom" in err
        # Nothing was deleted: the dirty file is still there.
        assert os.path.exists(os.path.join(clone, "wip.txt"))


class TestWorkingTreeDirtyTriple:
    def test_clean(self, api, repo_pair) -> None:
        _, clone = repo_pair
        assert api._working_tree_dirty(clone) == (True, False, None)

    def test_dirty(self, api, repo_pair) -> None:
        _, clone = repo_pair
        with open(os.path.join(clone, "x.txt"), "w", encoding="utf-8") as fh:
            fh.write("d\n")
        ok, dirty, listing = api._working_tree_dirty(clone)
        assert (ok, dirty) == (True, True)
        assert "x.txt" in listing

    def test_status_failure(self, api, tmp_path) -> None:
        not_a_repo = str(tmp_path / "empty")
        os.makedirs(not_a_repo)
        ok, dirty, listing = api._working_tree_dirty(not_a_repo)
        assert ok is False and dirty is True
        assert listing
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest agents/coding_team/tests/test_branch_prep_recovery.py -q`
Expected: FAIL — `_recover_dirty_tree` missing; `_working_tree_dirty` returns a 2-tuple.

- [ ] **Step 3: Implement**

Replace `_working_tree_dirty()`:

```python
def _working_tree_dirty(repo_path: str) -> Tuple[bool, bool, Optional[str]]:
    """Inspect the working tree.

    Postconditions:
        - Returns (status_ok, dirty, listing). status_ok=False means
          `git status` itself failed (state unknowable — callers must fail
          closed, never attempt recovery); listing then carries the error.
        - When status_ok, listing is bounded porcelain output (or None when
          clean) so conflicting paths can be surfaced without dumping file
          contents.
    """
    rc, msg = _git(repo_path, "status", "--porcelain")
    if rc != 0:
        return False, True, msg or "git status failed"
    return True, bool(msg.strip()), msg if msg.strip() else None
```

Add after `_latest_issue_rescue_ref()`:

```python
def _recover_dirty_tree(
    repo_path: str, marker: Optional[int], issue_number: Optional[int], listing: str
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Commit or preserve a dirty working tree before branch prep.

    Same-issue work (marker == issue_number, HEAD on a real branch) is
    committed in place so it can seed continuation; anything else — foreign
    issue, unknown attribution, detached HEAD — is moved onto a rescue
    branch. Work is never deleted.

    Preconditions:
        - The working tree is dirty and `git status` succeeded (callers
          gate on _working_tree_dirty's status_ok).
    Postconditions:
        - On success (error is None) the working tree is clean and the prior
          dirty state is committed on the returned-or-noted branch; wip_tip
          names the continuation seed candidate when the work belongs to
          issue_number, else None; note is operator-facing.
        - On failure (error set) nothing has been deleted.
    """
    same_issue = marker is not None and issue_number is not None and marker == issue_number
    rc, head = _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
    head_branch = head.strip() if rc == 0 else "HEAD"
    on_branch = head_branch not in ("", "HEAD")

    if same_issue and on_branch:
        ok, msg = commit_working_tree(
            repo_path,
            f"wip: recover uncommitted changes from interrupted run (issue {issue_number})",
        )
        if not ok:
            return None, None, msg
        note = f"♻️ Recovered uncommitted changes from an interrupted run (committed on `{head_branch}`)."
        return head_branch, note, None

    rescue = _rescue_branch_name(repo_path, marker)
    if rescue is None:
        return None, None, "could not allocate a rescue branch name"
    rc, msg = _git(repo_path, "checkout", "-b", rescue, "--")
    if rc != 0:
        return None, None, f"rescue branch creation failed: {msg}"
    was = f" (was on `{head_branch}`)" if on_branch else ""
    ok, msg = commit_working_tree(
        repo_path, f"wip: rescue uncommitted changes from interrupted run{was}\n\n{listing}".rstrip()
    )
    if not ok:
        return None, None, f"rescue commit failed: {msg}"
    wip_tip = rescue if same_issue else None
    note = f"♻️ Recovered uncommitted changes from an interrupted run; preserved on local branch `{rescue}`."
    return wip_tip, note, None
```

Update the two existing monkeypatches in `test_github_source.py` (in `TestGitCredentialThreading`) from
`monkeypatch.setattr(api, "_working_tree_dirty", lambda p: (False, None))` to
`monkeypatch.setattr(api, "_working_tree_dirty", lambda p: (True, False, None))`.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest agents/coding_team/tests/test_branch_prep_recovery.py -q`
Expected: 18 passed.

- [ ] **Step 5: Commit**

```bash
git -C /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams add backend/agents/coding_team/api/main.py backend/agents/coding_team/tests/test_branch_prep_recovery.py backend/agents/coding_team/tests/test_github_source.py
git -C /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams commit -m "feat: deterministic dirty-tree recovery with attribution-aware rescue branches"
```

---

### Task 4: Continuation-aware `_prepare_issue_branch`

**Files:**
- Modify: `backend/agents/coding_team/api/main.py` (rewrite `_prepare_issue_branch()`; new `_preserve_if_would_orphan()`)
- Modify: `backend/agents/coding_team/tests/test_github_source.py` (`TestPrepareIssueBranch` rewrite; `TestGitCredentialThreading` prep tests; `patched_app` prep stub)
- Test: `backend/agents/coding_team/tests/test_branch_prep_recovery.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `test_branch_prep_recovery.py`:

```python
# ---------------------------------------------------------------------------
# _prepare_issue_branch: recovery + continuation end to end
# ---------------------------------------------------------------------------


def _prep(api, clone: str, issue: int = 7):
    return api._prepare_issue_branch(clone, "origin", "main", f"khala/issue-{issue}", None, issue_number=issue)


class TestPrepareIssueBranchRecovery:
    def test_fresh_clean_checkout_unchanged_behavior(self, api, repo_pair) -> None:
        _, clone = repo_pair
        ok, err, notes = _prep(api, clone)
        assert ok is True, err
        assert notes == []
        assert _must(clone, "rev-parse", "--abbrev-ref", "HEAD") == "khala/issue-7"
        assert _must(clone, "rev-parse", "khala/issue-7") == _must(clone, "rev-parse", "origin/main")
        assert _branch_exists(clone, "khala/rescue/*") is None

    def test_marker_written_on_success(self, api, repo_pair) -> None:
        _, clone = repo_pair
        ok, _, _ = _prep(api, clone)
        assert ok is True
        assert api._read_active_issue(clone) == 7

    def test_dirty_same_issue_recovers_and_continues(self, api, repo_pair) -> None:
        """The headline scenario: interrupted mid-run, job deleted, new job started."""
        _, clone = repo_pair
        # Simulate the interrupted run: prep'd for issue 7, progressed on development.
        ok, _, _ = _prep(api, clone)
        assert ok is True
        _must(clone, "checkout", "-q", "development")
        _commit_file(clone, "src/progress.py", "x = 1\n", "task progress")
        with open(os.path.join(clone, "specs/notes.md"), "w", encoding="utf-8") as fh:
            os.makedirs(os.path.join(clone, "specs"), exist_ok=True)
            fh.write("open questions\n")
        # (no _clear_active_issue: the job died)

        ok, err, notes = _prep(api, clone)
        assert ok is True, err
        files = _must(clone, "ls-tree", "-r", "--name-only", "khala/issue-7")
        assert "src/progress.py" in files
        assert "specs/notes.md" in files
        assert any("Recovered" in n for n in notes)
        assert any("Continuing" in n for n in notes)
        assert api._working_tree_dirty(clone) == (True, False, None)

    def test_dirty_foreign_issue_rescued_not_continued(self, api, repo_pair) -> None:
        _, clone = repo_pair
        ok, _, _ = _prep(api, clone, issue=5)
        assert ok is True
        with open(os.path.join(clone, "foreign.txt"), "w", encoding="utf-8") as fh:
            fh.write("issue 5 work\n")

        ok, err, notes = _prep(api, clone, issue=7)
        assert ok is True, err
        assert "foreign.txt" not in _must(clone, "ls-tree", "-r", "--name-only", "khala/issue-7")
        rescued = _branch_exists(clone, "khala/rescue/issue-5-*")
        assert rescued is not None
        assert "foreign.txt" in _must(clone, "ls-tree", "-r", "--name-only", rescued)

    def test_status_failure_fails_closed(self, api, repo_pair, monkeypatch) -> None:
        _, clone = repo_pair
        monkeypatch.setattr(api, "_working_tree_dirty", lambda p: (False, True, "git status failed"))
        ok, err, _ = _prep(api, clone)
        assert ok is False
        assert "git status failed" in err

    def test_recovery_failure_fails_closed_with_both_messages(self, api, repo_pair, monkeypatch) -> None:
        _, clone = repo_pair
        with open(os.path.join(clone, "wip.txt"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        monkeypatch.setattr(api, "commit_working_tree", lambda *_a, **_kw: (False, "boom"))
        ok, err, _ = _prep(api, clone)
        assert ok is False
        assert "uncommitted changes" in err and "boom" in err
        assert os.path.exists(os.path.join(clone, "wip.txt"))


class TestPrepareIssueBranchContinuation:
    def test_continues_from_local_issue_branch(self, api, repo_pair) -> None:
        _, clone = repo_pair
        _must(clone, "checkout", "-q", "-b", "khala/issue-7", "origin/main")
        _commit_file(clone, "done.py", "ok = True\n", "finished work")
        _must(clone, "checkout", "-q", "main")

        ok, err, notes = _prep(api, clone)
        assert ok is True, err
        assert "done.py" in _must(clone, "ls-tree", "-r", "--name-only", "khala/issue-7")
        assert any("Continuing" in n for n in notes)

    def test_continues_from_remote_issue_branch(self, api, repo_pair) -> None:
        remote, clone = repo_pair
        # Previous job pushed khala/issue-7 then died; local checkout knows nothing.
        _must(remote, "checkout", "-q", "-b", "khala/issue-7")
        _commit_file(remote, "pushed.py", "ok = True\n", "pushed work")
        _must(remote, "checkout", "-q", "main")

        ok, err, notes = _prep(api, clone)
        assert ok is True, err
        assert "pushed.py" in _must(clone, "ls-tree", "-r", "--name-only", "khala/issue-7")
        assert any("Continuing" in n for n in notes)

    def test_continues_from_issue_rescue_ref(self, api, repo_pair) -> None:
        _, clone = repo_pair
        _must(clone, "checkout", "-q", "-b", "khala/rescue/issue-7-20260101-000000", "origin/main")
        _commit_file(clone, "rescued.py", "ok = True\n", "rescued work")
        _must(clone, "checkout", "-q", "main")

        ok, err, notes = _prep(api, clone)
        assert ok is True, err
        assert "rescued.py" in _must(clone, "ls-tree", "-r", "--name-only", "khala/issue-7")

    def test_local_branch_preferred_over_remote(self, api, repo_pair) -> None:
        remote, clone = repo_pair
        _must(remote, "checkout", "-q", "-b", "khala/issue-7")
        _commit_file(remote, "remote.py", "r = 1\n", "remote work")
        _must(remote, "checkout", "-q", "main")
        _must(clone, "checkout", "-q", "-b", "khala/issue-7", "origin/main")
        _commit_file(clone, "local.py", "l = 1\n", "local work")
        _must(clone, "checkout", "-q", "main")

        ok, err, _ = _prep(api, clone)
        assert ok is True, err
        files = _must(clone, "ls-tree", "-r", "--name-only", "khala/issue-7")
        assert "local.py" in files


class TestOrphanPrevention:
    def test_foreign_development_progress_preserved_before_reset(self, api, repo_pair) -> None:
        _, clone = repo_pair
        ok, _, _ = _prep(api, clone, issue=5)
        assert ok is True
        _must(clone, "checkout", "-q", "development")
        _commit_file(clone, "issue5.py", "x = 5\n", "issue 5 progress")
        dev_tip = _must(clone, "rev-parse", "development")

        ok, err, _ = _prep(api, clone, issue=7)
        assert ok is True, err
        # development was re-seeded from origin/main…
        assert _must(clone, "rev-parse", "development") == _must(clone, "rev-parse", "origin/main")
        # …but the old tip is still reachable from a rescue ref.
        reachable = _must(clone, "rev-list", "--all")
        assert dev_tip in reachable
        assert _branch_exists(clone, "khala/rescue/issue-5-*") is not None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest agents/coding_team/tests/test_branch_prep_recovery.py -q`
Expected: FAIL — `_prepare_issue_branch` takes no `issue_number` kwarg and returns a 2-tuple.

- [ ] **Step 3: Implement**

Add `_preserve_if_would_orphan()` after `_recover_dirty_tree()`:

```python
def _preserve_if_would_orphan(
    repo_path: str, branch: str, base_ref: str, seed: str, marker: Optional[int]
) -> Optional[str]:
    """Create a rescue ref for `branch` if resetting it would orphan commits.

    Invariant served: no reset performed by branch prep may make a commit
    unreachable.

    Postconditions:
        - Returns None when nothing needed preserving or a rescue ref now
          holds the branch tip; returns an error string when preservation
          was needed but failed (callers must fail closed).
    """
    if branch == seed:
        return None
    if not _is_ahead(repo_path, branch, base_ref):
        return None
    if _reachable_from(repo_path, branch, seed):
        return None
    name = _rescue_branch_name(repo_path, marker)
    if name is None:
        return f"could not allocate a rescue branch to preserve `{branch}`"
    rc, msg = _git(repo_path, "branch", name, branch)
    if rc != 0:
        return f"failed to preserve `{branch}` before reset: {msg}"
    logger.warning("Preserved %s on %s before reset (ahead of %s)", branch, name, base_ref)
    return None
```

Replace `_prepare_issue_branch()` entirely:

```python
def _prepare_issue_branch(
    repo_path: str,
    remote: str,
    default_branch: str,
    integration_branch: str,
    token: Optional[str] = None,
    issue_number: Optional[int] = None,
) -> Tuple[bool, Optional[str], List[str]]:
    """Prepare development + integration branches, recovering interrupted state.

    Dirty trees are recovered (same-issue work committed in place, foreign
    work preserved on khala/rescue/* branches), the integration branch is
    seeded from the best prior-progress tip so a new job picks up where the
    previous one left off, and no reset may orphan commits.

    Preconditions:
        - repo_path is a git checkout; ref arguments may be untrusted.
    Postconditions (success):
        - integration_branch is checked out with a clean working tree;
          khala.active-issue records issue_number when provided; every commit
          reachable from a local branch on entry is still reachable from some
          local or remote ref; the returned notes describe recovery and
          continuation actions for operator-facing reporting.
    Postconditions (failure):
        - No uncommitted work has been deleted and no commit that was
          reachable on entry has become unreachable.
    """
    notes: List[str] = []
    marker = _read_active_issue(repo_path)

    status_ok, dirty, listing = _working_tree_dirty(repo_path)
    if not status_ok:
        return False, f"cannot inspect working tree: {listing}", notes
    wip_tip: Optional[str] = None
    if dirty:
        wip_tip, note, recover_err = _recover_dirty_tree(repo_path, marker, issue_number, listing or "")
        if recover_err:
            return (
                False,
                "working tree has uncommitted changes; clean it before retrying:\n"
                f"{listing}\n(automatic recovery failed: {recover_err})",
                notes,
            )
        if note:
            notes.append(note)

    # Defense-in-depth: reject ref names that could be parsed as git options.
    if not _is_safe_ref(default_branch):
        return False, f"unsafe default_branch ref: {default_branch!r}", notes
    if not _is_safe_ref(integration_branch):
        return False, f"unsafe integration_branch ref: {integration_branch!r}", notes

    # `fetch` is the only network op here (the checkouts below are local), so it
    # needs the credential. The clone was authenticated transiently by the
    # unified API; that auth is not persisted, so we re-supply it per fetch.
    auth_env = _git_auth_env(token) if token else None
    rc, msg = _git(repo_path, "fetch", "--", remote, default_branch, env=auth_env)
    if rc != 0:
        return False, msg, notes
    # The issue branch may exist remotely from a previous job that pushed
    # before dying; fetch it as a continuation candidate (absence is fine).
    _git(repo_path, "fetch", "--", remote, integration_branch, env=auth_env)

    base_ref = f"{remote}/{default_branch}"
    candidates: List[str] = []
    if marker is not None and issue_number is not None and marker == issue_number:
        candidates.append(wip_tip or DEVELOPMENT_BRANCH)
    candidates.append(integration_branch)
    candidates.append(f"{remote}/{integration_branch}")
    if issue_number is not None:
        rescue_ref = _latest_issue_rescue_ref(repo_path, issue_number)
        if rescue_ref:
            candidates.append(rescue_ref)
    seed = next((c for c in candidates if _is_ahead(repo_path, c, base_ref)), base_ref)

    if seed != base_ref:
        rc, count = _git(repo_path, "rev-list", "--count", f"{base_ref}..{seed}")
        ahead = count.strip() if rc == 0 else "?"
        notes.append(
            f"▶️ Continuing issue from previous progress: `{seed}` ({ahead} commits ahead of `{default_branch}`)."
        )

    # Invariant: no reset below may make commits unreachable.
    for branch in (DEVELOPMENT_BRANCH, integration_branch):
        preserve_err = _preserve_if_would_orphan(repo_path, branch, base_ref, seed, marker)
        if preserve_err:
            return False, preserve_err, notes

    rc, msg = _git(repo_path, "checkout", "-B", DEVELOPMENT_BRANCH, seed, "--")
    if rc != 0:
        return False, msg, notes
    rc, msg = _git(repo_path, "checkout", "-B", integration_branch, "--")
    if rc != 0:
        return False, msg, notes
    if issue_number is not None:
        _write_active_issue(repo_path, issue_number)
    return True, None, notes
```

- [ ] **Step 4: Update the existing tests that pin the old contract**

In `test_github_source.py`:

(a) `patched_app` fixture: `monkeypatch.setattr(api_main, "_prepare_issue_branch", lambda *a, **kw: (True, None))` → `(True, None, [])`.

(b) `TestPrepareIssueBranch.test_dirty_tree_aborts` — the dirty tree now recovers; rewrite as:

```python
    def test_dirty_tree_recovered_to_rescue_branch(self, api, tmp_path) -> None:
        """Uncommitted unattributed changes are preserved, then prep proceeds."""
        repo = self._init_repo(tmp_path)
        with open(f"{repo}/README.md", "a") as fh:
            fh.write("dirty\n")

        ok, msg, notes = api._prepare_issue_branch(repo, "origin", "main", "khala/issue-9")
        assert ok is True, msg
        assert any("khala/rescue/" in n for n in notes)
        import subprocess

        status = subprocess.run(
            ["git", "-C", repo, "status", "--porcelain"], capture_output=True, text=True
        ).stdout.strip()
        assert status == ""
```

(c) `test_clean_tree_succeeds`, `test_unsafe_default_branch_rejected`, `test_unsafe_integration_branch_rejected`: change the unpack from `ok, msg = …` to `ok, msg, _notes = …` (assertions unchanged).

(d) `TestGitCredentialThreading.test_prepare_issue_branch_passes_auth_env_to_fetch` and `…_without_token_uses_no_auth_env`: the call now returns a 3-tuple and prep issues additional non-fetch git calls (marker read, candidate probes, config write), so locate fetches by name instead of position:

```python
    def test_prepare_issue_branch_passes_auth_env_to_fetch(self, api, monkeypatch) -> None:
        calls = []

        def fake_git(repo_path, *args, timeout=120.0, env=None):
            calls.append((args, env))
            return 0, ""

        monkeypatch.setattr(api, "_working_tree_dirty", lambda p: (True, False, None))
        monkeypatch.setattr(api, "_git", fake_git)
        ok, msg, _notes = api._prepare_issue_branch("/repo", "origin", "main", "khala/issue-1", "tok-123")
        assert ok is True, msg
        fetches = [(args, env) for args, env in calls if args[0] == "fetch"]
        assert len(fetches) == 2  # base branch + issue-branch continuation candidate
        for _args, env in fetches:
            assert env is not None
            # b64("x-access-token:tok-123")
            assert env["GIT_CONFIG_VALUE_0"] == "Authorization: Basic eC1hY2Nlc3MtdG9rZW46dG9rLTEyMw=="
        # Local-only git ops never carry the credential.
        assert all(env is None for args, env in calls if args[0] != "fetch")

    def test_prepare_issue_branch_without_token_uses_no_auth_env(self, api, monkeypatch) -> None:
        calls = []

        def fake_git(repo_path, *args, timeout=120.0, env=None):
            calls.append((args, env))
            return 0, ""

        monkeypatch.setattr(api, "_working_tree_dirty", lambda p: (True, False, None))
        monkeypatch.setattr(api, "_git", fake_git)
        ok, _msg, _notes = api._prepare_issue_branch("/repo", "origin", "main", "khala/issue-1")
        assert ok is True
        assert all(env is None for _, env in calls)
```

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/python -m pytest agents/coding_team/tests/test_branch_prep_recovery.py agents/coding_team/tests/test_github_source.py -q`
Expected: all pass (29 in the recovery file).

- [ ] **Step 6: Commit**

```bash
git -C /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams add backend/agents/coding_team/api/main.py backend/agents/coding_team/tests/test_branch_prep_recovery.py backend/agents/coding_team/tests/test_github_source.py
git -C /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams commit -m "feat: continuation-aware branch prep with orphan-prevention invariant"
```

---

### Task 5: Hook wiring — notes comments + marker lifecycle

**Files:**
- Modify: `backend/agents/coding_team/api/main.py` (`_run_with_github_hooks()`)
- Modify: `backend/agents/coding_team/tests/test_github_source.py` (`_stub_heavy_modules` + new lifecycle tests)

- [ ] **Step 1: Write the failing tests**

In `test_github_source.py`, extend `_stub_heavy_modules()` so the synthetic git_utils stub satisfies api.main's new import, and tolerate an already-installed stale stub (append at the end of the function):

```python
    gu_mod = sys.modules["software_engineering_team.shared.git_utils"]
    if not hasattr(gu_mod, "commit_working_tree"):
        gu_mod.commit_working_tree = lambda *_a, **_kw: (True, "Committed")  # type: ignore[attr-defined]
```

Add a new test class after `TestGitCredentialThreading`:

```python
class TestActiveIssueMarkerLifecycle:
    """The marker must be cleared on every terminal path after a successful prep."""

    def _run(self, patched_app, monkeypatch, github_client, orchestrator=None):
        api = patched_app["api"]
        cleared: list[str] = []
        monkeypatch.setattr(api, "_clear_active_issue", lambda p: cleared.append(p))
        if orchestrator is not None:
            monkeypatch.setattr(api, "run_coding_team_orchestrator", orchestrator)
        patched_app["set_github"](github_client)
        resp = patched_app["client"].post("/run-from-github", json=_body(issue_number=3))
        assert resp.status_code == 200
        return cleared

    def test_cleared_on_success(self, patched_app, monkeypatch) -> None:
        client = _FakeClient(issues=[_issue(3)])
        cleared = self._run(patched_app, monkeypatch, client)
        assert cleared == [patched_app["repo_path"]]

    def test_cleared_when_orchestrator_raises(self, patched_app, monkeypatch) -> None:
        def boom(*_a, **_kw):
            raise RuntimeError("orchestrator died")

        client = _FakeClient(issues=[_issue(3)])
        cleared = self._run(patched_app, monkeypatch, client, orchestrator=boom)
        assert cleared == [patched_app["repo_path"]]

    def test_cleared_when_no_merged_tasks(self, patched_app, monkeypatch) -> None:
        def no_merge(_job_id, _repo, _plan, **kw):
            kw["update_job_fn"](status="completed", task_graph_snapshot=[])

        client = _FakeClient(issues=[_issue(3)])
        cleared = self._run(patched_app, monkeypatch, client, orchestrator=no_merge)
        assert cleared == [patched_app["repo_path"]]

    def test_prep_notes_posted_as_issue_comments(self, patched_app, monkeypatch) -> None:
        api = patched_app["api"]
        monkeypatch.setattr(api, "_clear_active_issue", lambda p: None)
        monkeypatch.setattr(
            api,
            "_prepare_issue_branch",
            lambda *a, **kw: (True, None, ["♻️ recovered", "▶️ continuing"]),
        )
        client = _FakeClient(issues=[_issue(3)])
        patched_app["set_github"](client)
        resp = patched_app["client"].post("/run-from-github", json=_body(issue_number=3))
        assert resp.status_code == 200
        bodies = [body for _n, body in client.comments]
        assert "♻️ recovered" in bodies
        assert "▶️ continuing" in bodies
```

Note: `_issue(...)` and `_body(...)` are the existing module-level helpers in this file; if `_issue` does not exist, mirror the construction used by the existing endpoint tests in the same file (an `Issue(number=3, title="Issue 3", body="", state="open", html_url="", labels=("ready",))`).

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest "agents/coding_team/tests/test_github_source.py::TestActiveIssueMarkerLifecycle" -q`
Expected: FAIL — comments missing / `_clear_active_issue` never called (notes loop and finally-clear not implemented).

- [ ] **Step 3: Implement in `_run_with_github_hooks()`**

Replace the prep call and wrap everything after a successful prep in `try`/`finally`:

```python
        prep_ok, prep_err, prep_notes = _prepare_issue_branch(
            request.repo_path, request.remote, base, integration_branch, token, issue_number=num
        )
        if not prep_ok:
            _record_failure(client, owner, repo, num, job_id, f"branch prep failed: {prep_err}")
            return
        for note in prep_notes:
            _safe_comment(client, owner, repo, num, note)

        try:
            # … existing body from `try: run_coding_team_orchestrator(…)` through
            # the final `_safe_comment(...Reusing existing draft PR...)`,
            # re-indented one level, otherwise unchanged …
        finally:
            # A set marker means "job mid-flight on this checkout"; clearing it
            # on every terminal path keeps later leftover-attribution honest.
            _clear_active_issue(request.repo_path)
```

- [ ] **Step 4: Run to verify pass + full coding-team suite**

Run: `.venv/bin/python -m pytest agents/coding_team/tests/ -q`
Expected: all pass except the two pre-existing `test_tech_lead_plan_to_graph.py` failures (verify they also fail with `git stash` on a clean tree before accepting them).

- [ ] **Step 5: Commit**

```bash
git -C /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams add backend/agents/coding_team/api/main.py backend/agents/coding_team/tests/test_github_source.py
git -C /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams commit -m "feat: surface recovery notes on the issue and clear the active-issue marker on terminal paths"
```

---

### Task 6: Docs, lint, full verification

**Files:**
- Modify: `CLAUDE.md` (env table)

- [ ] **Step 1: Add env rows to CLAUDE.md**

In the "Key Environment Variables" table, after the `GITHUB_API_URL` row, add:

```markdown
| `GIT_COMMIT_USER_NAME` | Author/committer name for every git commit platform code makes (SE pipeline, coding team, agent git tools). Default `Khala`. Blank values fall back to the default; natively-exported `GIT_AUTHOR_*`/`GIT_COMMITTER_*` win over this setting. |
| `GIT_COMMIT_USER_EMAIL` | Author/committer email for platform git commits. Default `brandon.kindred@gmail.com`. Same precedence rules as `GIT_COMMIT_USER_NAME`. |
```

- [ ] **Step 2: Lint + format everything touched**

```bash
.venv/bin/python -m ruff check agents/software_engineering_team/shared/git_utils.py agents/software_engineering_team/tests/test_git_identity.py agents/coding_team/api/main.py agents/coding_team/tests/test_branch_prep_recovery.py agents/coding_team/tests/test_github_source.py
.venv/bin/python -m ruff format agents/software_engineering_team/shared/git_utils.py agents/software_engineering_team/tests/test_git_identity.py agents/coding_team/api/main.py agents/coding_team/tests/test_branch_prep_recovery.py agents/coding_team/tests/test_github_source.py
```

Expected: no findings; re-run the suites if format changed files.

- [ ] **Step 3: Full verification + coverage on changed modules**

```bash
.venv/bin/python -m pytest agents/coding_team/tests/ agents/software_engineering_team/tests/test_git_identity.py agents/software_engineering_team/tests/test_git_utils.py agents/software_engineering_team/tests/test_git_utils_more.py agents/software_engineering_team/tests/test_git_utils_extra.py unified_api/tests/test_integrations_github_route.py -q
.venv/bin/python -m pytest agents/coding_team/tests/test_branch_prep_recovery.py agents/software_engineering_team/tests/test_git_identity.py --cov=coding_team.api.main --cov=software_engineering_team.shared.git_utils --cov-report=term-missing -q
```

Expected: green (modulo the two pre-existing tech-lead failures); ≥90 % line coverage on the changed code paths.

- [ ] **Step 4: Commit**

```bash
git -C /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams add CLAUDE.md
git -C /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams commit -m "docs: GIT_COMMIT_USER_NAME / GIT_COMMIT_USER_EMAIL environment reference"
```

---

## Plan Self-Review

- **Spec coverage:** §5 identity → Task 1; §6.1 marker → Tasks 2/5; §6.2 recovery → Task 3; §6.3 continuation + orphan prevention → Task 4; §6.4 transparency → Task 5; §7 tests distributed per task; §9 docs → Task 6. The §6.2 collision-suffix bound and §6.3 newest-rescue-ref ordering are implemented in `_rescue_branch_name`/`_latest_issue_rescue_ref` (Task 2).
- **Placeholder scan:** every code step carries complete code; the one elision (Task 5 Step 3 "existing body … re-indented") refers to code that must not change, with exact start/end anchors.
- **Type consistency:** `_working_tree_dirty` 3-tuple is consumed consistently (Tasks 3-5 and all monkeypatch updates); `_prepare_issue_branch` 3-tuple return updated at every caller (hooks, patched_app stub, both test classes); helper names match between definition (Task 2) and use (Tasks 3-4).
