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
    """Commit ``contents`` to ``filename`` on ``branch`` (created via
    ``checkout -B`` if it doesn't exist yet) in the on-disk git repo at
    ``repo``, and return the resulting commit SHA, leaving the checkout back
    on ``main``.

    Shared by ``test_coding_team_github_source.py``'s ``TestPrepareIssueBranch``
    and ``test_coding_team_github_branch_prep_activity.py``'s
    ``expected_head_sha`` tests -- both exercise ``_prepare_issue_branch`` /
    ``github_branch_prep_activity`` against a real on-disk repo built by
    each file's own local ``_init_repo`` and need a way to simulate a PR
    branch moving to a known SHA.
    """

    def _run(*args: str) -> None:
        subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True)

    _run("checkout", "-q", "-B", branch)
    with open(f"{repo}/{filename}", "w") as fh:
        fh.write(contents)
    _run("add", filename)
    _run("commit", "-q", "--no-gpg-sign", "-m", f"{branch}: {filename}")
    sha = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    _run("checkout", "-q", "main")
    return sha
