"""
Shared Setup-phase implementation for the code-v2 teams.

``run_setup`` and ``configure_quality_tooling`` were identical between the
backend and frontend teams; only the stack-specific ``_ensure_linting_configured``
/ ``_ensure_testing_configured`` hooks differ. Those hooks stay in each team's
``phases/setup.py`` and are injected here. ``commit_paths`` is likewise injected
(rather than imported here) so the team module remains the monkeypatch boundary
for the scaffolding-commit tests.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Set, Tuple

from shared.git.git_utils import (
    ensure_development_branch,
    initialize_new_repo,
    write_files_and_commit,
)
from software_engineering_team.shared.v2_models import SetupResult

logger = logging.getLogger(__name__)

# Callable that ensures a lint/test tool is configured, recording touched paths.
EnsureHook = Callable[[Path, Set[str]], bool]
# Callable matching ``shared.git_utils.commit_paths``.
CommitPaths = Callable[..., Tuple[bool, str]]


def _commit_scaffolding(
    path: Path, scaffolding_paths: Set[str], *, commit_paths: CommitPaths
) -> None:
    """Commit only the lint/test scaffolding setup wrote onto the current branch.

    Setup runs on ``development`` and may write linting/testing config and a
    ``tests/`` skeleton. Leaving those changes uncommitted means a later
    revision pass regenerates them as *untracked* files on ``development``; the
    development agent's subsequent ``git checkout`` of the review feature branch
    (which already tracks those same paths) then aborts because the checkout
    would overwrite untracked files, failing the task before it can apply the
    requested revision. Committing the scaffolding to ``development`` keeps the
    working tree clean and makes the idempotent ``_ensure_*_configured`` checks
    a no-op on every subsequent pass.

    Only the paths setup actually created/updated this run are committed (scoped
    via ``commit_paths``), so unrelated uncommitted work that happened to be in
    the tree is never swept into the scaffolding commit — while a config file
    setup edited is still committed even if it was already dirty for unrelated
    reasons, since leaving setup's edit uncommitted would re-block the checkout.

    Preconditions:
        - ``path`` is a git repository checked out on the development branch.
        - ``scaffolding_paths`` are repo-relative paths setup wrote this run.
        - ``commit_paths`` matches ``shared.git_utils.commit_paths``.

    Postconditions:
        - The named scaffolding paths are committed (or no-op when empty/clean);
          other working-tree changes are left untouched. A failed commit never
          fails setup, but it is logged (not silently swallowed) so the later
          feature-branch checkout conflict it can cause stays diagnosable.
    """
    if not scaffolding_paths:
        return
    try:
        committed, detail = commit_paths(
            path,
            sorted(scaffolding_paths),
            "chore: configure linting and testing scaffolding",
        )
    except Exception as e:  # noqa: BLE001 - scaffolding commit is best-effort
        logger.warning("Could not commit setup scaffolding: %s", e)
        return
    if not committed:
        # A non-raising failure (e.g. a repo pre-commit/commit-msg hook rejecting
        # the synthetic commit) leaves the scaffolding uncommitted; surface it so
        # the later feature-branch checkout conflict this guards against is
        # diagnosable instead of silently reappearing.
        logger.warning(
            "Setup scaffolding was not committed (%s); it remains uncommitted on the "
            "current branch and may cause a later feature-branch checkout conflict.",
            detail,
        )


def _ensure_readme_with_title(path: Path, title: str) -> None:
    """Write or prepend project title to README.md and commit if possible.

    Preconditions:
        ``path`` is a git repository; ``title`` is a non-empty string.
    Postconditions:
        ``README.md`` begins with ``# {title}``; a failed commit is logged, not
        raised.
    """
    readme = path / "README.md"
    content = f"# {title}\n\n"
    if readme.exists():
        existing = readme.read_text(encoding="utf-8")
        if existing.strip() and not existing.lstrip().startswith("#"):
            content = content + existing
        else:
            content = content + existing.lstrip()
    readme.write_text(content, encoding="utf-8")
    # Commit if we have git and changes
    try:
        write_files_and_commit(path, {"README.md": content}, "docs: add README with project title")
    except Exception as e:
        logger.warning("Could not commit README: %s", e)


def configure_quality_tooling_impl(
    repo_path: Path,
    *,
    ensure_linting: EnsureHook,
    ensure_testing: EnsureHook,
    commit_paths: CommitPaths,
) -> Tuple[bool, bool]:
    """Ensure lint/test scaffolding exists on the CURRENT branch and commit it.

    Setup commits the scaffolding to ``development``, but the coding-team handoff
    creates the review feature branch *before* setup runs, so that branch does
    not inherit the committed scaffolding. The development agent calls this after
    checking out the feature branch so the branch it actually edits carries the
    lint/test config — otherwise the pre-flight check (and later quality gates)
    fail on a config-less branch. Idempotent: a branch that already has the
    config produces no writes and no commit.

    Preconditions:
        - ``repo_path`` is a git repository checked out on the branch to configure.
        - ``ensure_linting`` / ``ensure_testing`` are the team's stack-specific hooks.

    Postconditions:
        - Linting and testing are configured on the current branch and any newly
          written scaffolding is committed to it. Returns ``(lint_ok, test_ok)``.
    """
    path = Path(repo_path).resolve()
    scaffolding: Set[str] = set()
    lint_ok = ensure_linting(path, scaffolding)
    test_ok = ensure_testing(path, scaffolding)
    _commit_scaffolding(path, scaffolding, commit_paths=commit_paths)
    return lint_ok, test_ok


def run_setup_impl(
    *,
    repo_path: Path,
    task_title: str,
    configure_quality_tooling: Callable[[Path], Tuple[bool, bool]],
) -> SetupResult:
    """Ensure the repository is initialized and ready for development.

    - If the path is not a git repo: git init, create README.md with project title,
      initial commit, rename master to main if needed, create development branch.
    - If already a repo: ensure development branch exists and is checked out;
      optionally ensure README exists (create minimal one if missing).
    - Always verifies linting and testing are configured before returning.

    Preconditions:
        ``configure_quality_tooling`` is the team's ``(path) -> (lint_ok, test_ok)``
        wrapper (so its stack-specific hooks and monkeypatch boundary apply).
    Postconditions:
        Returns a ``SetupResult`` describing what setup did; on init/branch
        failure returns early with a ``"Setup failed: …"`` summary.
    """
    result = SetupResult()
    path = Path(repo_path).resolve()

    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)

    if not (path / ".git").exists():
        ok, msg = initialize_new_repo(path)
        if not ok:
            result.summary = f"Setup failed: {msg}"
            logger.error("Setup: %s", result.summary)
            return result
        result.repo_initialized = True
        result.master_renamed_to_main = True
        result.branch_created = True
        result.readme_created = True  # initialize_new_repo creates README.md
        # Update README with project title if provided
        if task_title:
            _ensure_readme_with_title(path, task_title)

        # Ensure linting and testing are configured before any coding begins
        result.linting_configured, result.testing_configured = configure_quality_tooling(path)

        result.summary = f"Initialized repo: {msg}"
        logger.info("Setup: %s", result.summary)
        return result

    # Already a git repo: ensure development branch and README
    ok, msg = ensure_development_branch(path)
    if not ok:
        result.summary = f"Setup failed: {msg}"
        logger.error("Setup: %s", result.summary)
        return result
    if "Created branch" in msg:
        result.branch_created = True
    if not (path / "README.md").exists() and task_title:
        _ensure_readme_with_title(path, task_title)
        result.readme_created = True

    # Ensure linting and testing are configured before any coding begins
    result.linting_configured, result.testing_configured = configure_quality_tooling(path)

    result.summary = msg or "Repo ready; on development branch."
    logger.info(
        "Setup: %s (lint=%s, test=%s)",
        result.summary,
        result.linting_configured,
        result.testing_configured,
    )
    return result
