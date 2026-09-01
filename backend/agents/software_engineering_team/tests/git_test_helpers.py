"""Plain helper functions for tests that exercise real on-disk git checkouts.

Kept out of ``conftest.py`` — pytest discourages using conftest as a shared
library rather than for fixture discovery — so both ``conftest.py`` (whose
own fixtures/helpers need them) and test modules can import these directly.
"""

from __future__ import annotations

import base64
import subprocess


def _expected_basic_header(token: str) -> str:
    """Expected git auth header for a fake token, built at runtime so a
    credential-shaped Base64 literal never appears in source — secret
    scanners (GitGuardian etc.) flag the pattern regardless of how fake
    the values are."""
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"Authorization: Basic {encoded}"


def _commit_on_branch(repo: str, branch: str, filename: str, contents: str) -> str:
    """Commit ``contents`` to ``filename`` on ``branch`` in the on-disk git repo
    at ``repo``, and return the resulting commit SHA, leaving the checkout back
    on ``main``.

    Preconditions:
        - ``repo`` is an existing on-disk git checkout with a ``main`` branch
          already checked out (or at least existing) — this helper always
          returns to ``main`` afterwards, so a repo without one leaves the
          checkout in an unexpected state.
        - ``repo`` already has a committer identity configured (``git config
          user.email``/``user.name``, local or global) — this helper does not
          set one itself, and ``git commit`` fails without it. Both current
          callers' own ``_init_repo`` helpers set this on the repo they build
          before ever calling this function.

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
        """Run ``git -C repo <args>``, returning stdout stripped.

        Unlike a bare ``subprocess.run(..., check=True)``, a failure raises an
        ``AssertionError`` that includes the command and stderr — a plain
        ``CalledProcessError`` only shows the exit code, which makes a failing
        test hard to diagnose from CI output alone.
        """
        result = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError(
                f"git -C {repo} {' '.join(args)} failed (exit {result.returncode}): {result.stderr}"
            )
        return result.stdout.strip()

    _run("checkout", "-q", "-B", branch)
    with open(f"{repo}/{filename}", "w") as fh:
        fh.write(contents)
    _run("add", filename)
    _run("commit", "-q", "--no-gpg-sign", "-m", f"{branch}: {filename}")
    sha = _run("rev-parse", "HEAD")
    _run("checkout", "-q", "main")
    return sha
