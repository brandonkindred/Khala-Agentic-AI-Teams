"""Regression tests for `git_ops._checkout_remote_matches`'s push-URL validation.

`_checkout_remote_matches` validates an operator-pinned checkout's `origin`
remote before code gets committed and pushed there. It must reject a checkout
whose PUSH URL (`git remote get-url --push origin`, which can be configured
separately from the fetch URL via `remote.origin.pushurl`) points at a
different repository, even when the fetch URL matches — otherwise `git push
origin` could push to an unrelated repo despite the fetch-URL check passing.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

# `coding_team_main` must be imported before `git_ops` in test modules that
# import `git_ops` directly: `git_ops` imports `coding_team_main` (as `_main`)
# at module scope, and `coding_team_main` imports several names FROM
# `git_ops` at module scope too — importing `git_ops` first hits Python
# mid-initializing it when `coding_team_main`'s import runs, since the two
# already have a circular top-level dependency (unrelated to this fix).
from software_engineering_team.api import coding_team_main as _main  # noqa: F401
from software_engineering_team.api import git_ops


def _patch_git(monkeypatch, responses: Dict[Tuple[str, ...], Tuple[int, str]]) -> List[Tuple[str, ...]]:
    """Stub `git_ops._git` to answer canned ``args -> (rc, out)`` responses.

    Returns the list of ``args`` tuples the stub was called with, in order,
    so a test can assert both the outcome and that the expected git
    subcommands were actually issued.
    """
    calls: List[Tuple[str, ...]] = []

    def _fake_git(repo_path: str, *args: str, timeout: float = 120.0, env=None):
        calls.append(args)
        return responses[args]

    monkeypatch.setattr(git_ops, "_git", _fake_git)
    return calls


def test_matching_fetch_and_push_url_passes(monkeypatch) -> None:
    calls = _patch_git(
        monkeypatch,
        {
            ("remote", "get-url", "origin"): (0, "https://github.com/acme/widget.git"),
            ("remote", "get-url", "--push", "origin"): (0, "https://github.com/acme/widget.git"),
        },
    )

    assert git_ops._checkout_remote_matches("/repo", "acme", "widget") is True
    assert ("remote", "get-url", "origin") in calls
    assert ("remote", "get-url", "--push", "origin") in calls


def test_mismatched_push_url_fails_even_when_fetch_url_matches(monkeypatch) -> None:
    """A `remote.origin.pushurl` pointed at a different repo must fail validation
    even though the fetch URL is correct — this is the bug the fix closes."""
    _patch_git(
        monkeypatch,
        {
            ("remote", "get-url", "origin"): (0, "https://github.com/acme/widget.git"),
            ("remote", "get-url", "--push", "origin"): (0, "https://github.com/evil/other.git"),
        },
    )

    assert git_ops._checkout_remote_matches("/repo", "acme", "widget") is False


def test_mismatched_fetch_url_fails_without_checking_push_url(monkeypatch) -> None:
    calls = _patch_git(
        monkeypatch,
        {
            ("remote", "get-url", "origin"): (0, "https://github.com/evil/other.git"),
        },
    )

    assert git_ops._checkout_remote_matches("/repo", "acme", "widget") is False
    # Short-circuits on the fetch-URL mismatch; never bothers checking push.
    assert ("remote", "get-url", "--push", "origin") not in calls


def test_push_url_lookup_failure_fails_closed(monkeypatch) -> None:
    _patch_git(
        monkeypatch,
        {
            ("remote", "get-url", "origin"): (0, "https://github.com/acme/widget.git"),
            ("remote", "get-url", "--push", "origin"): (1, "fatal: no such remote 'origin'"),
        },
    )

    assert git_ops._checkout_remote_matches("/repo", "acme", "widget") is False


def test_expected_host_is_threaded_through_to_both_checks(monkeypatch) -> None:
    """A GHES checkout (whose fetch/push URLs use the enterprise host, not
    github.com) validates when the caller passes its own `expected_host`."""
    _patch_git(
        monkeypatch,
        {
            ("remote", "get-url", "origin"): (0, "https://ghes.example.com/acme/widget.git"),
            ("remote", "get-url", "--push", "origin"): (0, "https://ghes.example.com/acme/widget.git"),
        },
    )

    assert (
        git_ops._checkout_remote_matches(
            "/repo", "acme", "widget", expected_host="ghes.example.com"
        )
        is True
    )
    # Without the expected_host override, the same GHES remote fails against
    # the github.com default.
    assert git_ops._checkout_remote_matches("/repo", "acme", "widget") is False
