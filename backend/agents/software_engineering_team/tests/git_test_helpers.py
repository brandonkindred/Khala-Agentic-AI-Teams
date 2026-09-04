"""Plain helper functions for tests that exercise real on-disk git checkouts.

Kept out of ``conftest.py`` — pytest discourages using conftest as a shared
library rather than for fixture discovery — so both ``conftest.py`` (whose
own fixtures/helpers need them) and test modules can import these directly.
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path


def expected_basic_header(token: str) -> str:
    """Expected git auth header for a fake token, built at runtime so a
    credential-shaped Base64 literal never appears in source — secret
    scanners (GitGuardian etc.) flag the pattern regardless of how fake
    the values are."""
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"Authorization: Basic {encoded}"


def _run_git(repo: str, *args: str) -> str:
    """Run ``git -C repo <args>`` and return stdout, stripped.

    The one git invocation every helper in this module goes through. Unlike a
    bare ``subprocess.run(..., check=True)``, a failure raises an
    ``AssertionError`` naming the full command and git's stderr — a plain
    ``CalledProcessError`` carries only the exit code, which says nothing about
    WHY, and a failing git helper in CI is nearly always diagnosed from that
    output alone.

    Preconditions:
        - ``repo`` is a path to an on-disk git checkout; ``args`` are the git
          subcommand and its arguments (no ``-C``, supplied here).
    Postconditions:
        - Returns the command's stdout with surrounding whitespace stripped.
    Raises:
        AssertionError: git exited non-zero; the message names the command,
            the exit code and stderr.
    """
    result = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(
            f"git -C {repo} {' '.join(args)} failed (exit {result.returncode}): {result.stderr}"
        )
    return result.stdout.strip()


def current_branch(repo: str) -> str:
    """Return ``repo``'s currently checked-out branch name.

    One shared spelling of the ``git rev-parse --abbrev-ref HEAD`` subprocess
    call the branch-prep tests assert on repeatedly, so the invocation (and its
    text handling) exists once for every test module instead of once per module.

    Preconditions:
        - ``repo`` is a git checkout with a resolvable HEAD.
    Postconditions:
        - Returns the abbreviated branch name, whitespace-stripped.
        - Raises ``AssertionError`` naming the checkout and git's stderr if
          ``rev-parse`` fails (see :func:`_run_git`, which both helpers in this
          module share so that reporting cannot diverge between them).
    """
    return _run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def commit_on_branch(repo: str, branch: str, filename: str, contents: str) -> str:
    """Commit ``contents`` to ``filename`` on ``branch`` in the on-disk git repo
    at ``repo``, and return the resulting commit SHA, leaving the checkout back
    on ``main``.

    Postconditions:
        - On SUCCESS, returns the new commit's SHA with the checkout back on
          ``main``.
        - On FAILURE (a failing ``add``/``commit`` raises ``AssertionError``),
          the checkout is STILL returned to ``main`` AND reset hard onto it
          before the error propagates — the "back on main" guarantee is
          unconditional and covers the index/worktree, not just HEAD, so one
          genuine failure cannot leave every later assertion in the calling
          test running against the wrong HEAD *or* against changes the failed
          commit left staged.
        - That reset covers TRACKED and INDEXED paths only. In the one failure
          mode where ``write_text`` succeeds but ``git add`` itself fails (for
          instance ``filename`` matches a ``.gitignore`` rule in the repo under
          test), the file is never indexed, so it — and any parent directory
          created for it — is left on disk as an untracked remnant. This is
          deliberate rather than cleaned up: the cleanup would have to run in
          the same ``finally`` block, where every git call raises
          ``AssertionError`` on failure, so a failing ``git clean`` would
          replace the original diagnostic with its own and hide the real cause.
          The remnant is harmless in practice — the calling test has already
          failed, and each caller builds its own throwaway repo per the
          preconditions above.

    Preconditions:
        - ``repo`` is an existing on-disk git checkout with ``main`` **checked
          out** — not merely existing. The new commit is based on the current
          HEAD, which this precondition pins to ``main``'s tip (the
          ``checkout -B`` note below depends on exactly that), and the helper
          always returns to ``main`` afterwards. If some OTHER branch is HEAD,
          the commit is parented on that branch's tip instead while the helper
          still reports success and returns to ``main``, so the returned SHA
          would not be reachable from ``main``; a repo with no ``main`` at all
          leaves the checkout in an unexpected state.
        - ``repo`` already has a committer identity configured (``git config
          user.email``/``user.name``, local or global) — this helper does not
          set one itself, and ``git commit`` fails without it. Both current
          callers' own ``_init_repo`` helpers set this on the repo they build
          before ever calling this function.
        - ``repo``'s working tree and index are CLEAN on entry (no uncommitted
          or staged changes the caller still needs). The failure path's
          ``reset --hard`` is unconditional, so anything a caller had staged or
          modified before calling would be discarded along with the failed
          commit's remnants. Both current callers pass a freshly built
          throwaway repo, which satisfies this trivially.
        - ``filename`` is repository-relative. It MAY name a nested path
          (``pkg/mod.py``): missing parent directories are created here, so a
          caller does not have to pre-create them just to commit a file.

    Uses ``git checkout -B <branch>``, which creates ``branch`` if it does not
    already exist but, when it DOES exist, resets it to the current HEAD
    (``main``, per the precondition above) rather than leaving its prior tip
    alone — this is not conditional on the branch being new.

    Shared by ``test_coding_team_github_source.py``'s ``TestPrepareIssueBranch``
    and ``test_coding_team_github_branch_prep_activity.py``'s
    ``expected_head_sha`` tests -- both exercise ``_prepare_issue_branch`` /
    ``github_branch_prep_activity`` against a real on-disk repo built by
    each file's own local ``_init_repo`` and need a way to simulate a PR
    branch moving to a known SHA.
    """

    def _run(*args: str) -> str:
        """This repo's binding of :func:`_run_git` (same failure reporting)."""
        return _run_git(repo, *args)

    _run("checkout", "-q", "-B", branch)
    # try/finally, not a trailing call: the "leaves the checkout back on main"
    # postcondition must hold on the FAILURE path too. A failing add/commit
    # otherwise strands the repo on `branch`, and every later assertion in the
    # calling test runs against the wrong HEAD -- turning one real failure into
    # a cascade that hides its own cause.
    try:
        target = Path(repo) / filename
        # A nested `filename` would otherwise fail with FileNotFoundError before
        # git is ever reached; creating parents makes the helper usable for
        # directory paths too, which is strictly more useful than documenting
        # the limitation.
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
        _run("add", filename)
        _run("commit", "-q", "--no-gpg-sign", "-m", f"{branch}: {filename}")
        sha = _run("rev-parse", "HEAD")
    finally:
        _run("checkout", "-q", "main")
        # A successful `add` followed by a failing `commit` leaves `filename`
        # staged, and `branch` was just reset to `main`'s tip so there is no
        # commit difference for `checkout main` to refuse -- git carries the
        # staged change straight onto `main`. A later commit_on_branch call in
        # the same test would then `checkout -B` with that stale change still
        # staged and sweep it into its own commit, returning a SHA whose tree
        # holds content no caller asked for. The precondition pins `repo` to a
        # freshly built checkout with `main` checked out, so discarding here
        # can only discard the failed commit's own remnants.
        _run("reset", "-q", "--hard", "main")
    return sha
