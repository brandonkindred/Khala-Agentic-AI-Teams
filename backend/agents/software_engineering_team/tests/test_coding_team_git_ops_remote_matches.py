"""Regression tests for `git_ops._checkout_remote_matches`'s push-URL validation.

`_checkout_remote_matches` validates an operator-pinned checkout's `origin`
remote before code gets committed and pushed there. It must reject a checkout
whose PUSH URL(s) (`git remote get-url --push --all origin` — `--all` because
git supports more than one `remote.origin.pushurl` entry and pushes to every
one of them, while bare `--push` only ever reports the first) point at a
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


def _patch_git(
    monkeypatch, responses: Dict[Tuple[str, ...], Tuple[int, str]]
) -> List[Tuple[str, ...]]:
    """Stub `git_ops._git` to answer canned ``args -> (rc, out)`` responses.

    Returns the list of ``args`` tuples the stub was called with, in order,
    so a test can assert both the outcome and that the expected git
    subcommands were actually issued.

    An invocation with no canned response fails the test with an
    ``AssertionError`` naming the unexpected args, rather than a bare
    ``KeyError`` that says nothing about which git call went unstubbed.
    """
    calls: List[Tuple[str, ...]] = []

    def _fake_git(repo_path: str, *args: str, timeout: float = 120.0, env=None):
        calls.append(args)
        if args not in responses:
            raise AssertionError(f"Unexpected git invocation: {args!r}")
        return responses[args]

    monkeypatch.setattr(git_ops, "_git", _fake_git)
    return calls


def test_matching_fetch_and_push_url_passes(monkeypatch) -> None:
    calls = _patch_git(
        monkeypatch,
        {
            ("remote", "get-url", "origin"): (0, "https://github.com/acme/widget.git"),
            ("remote", "get-url", "--push", "--all", "origin"): (
                0,
                "https://github.com/acme/widget.git",
            ),
        },
    )

    assert git_ops._checkout_remote_matches("/repo", "acme", "widget") is True
    assert ("remote", "get-url", "origin") in calls
    assert ("remote", "get-url", "--push", "--all", "origin") in calls


def test_mismatched_push_url_fails_even_when_fetch_url_matches(monkeypatch) -> None:
    """A `remote.origin.pushurl` pointed at a different repo must fail validation
    even though the fetch URL is correct — this is the bug the fix closes."""
    _patch_git(
        monkeypatch,
        {
            ("remote", "get-url", "origin"): (0, "https://github.com/acme/widget.git"),
            ("remote", "get-url", "--push", "--all", "origin"): (
                0,
                "https://github.com/evil/other.git",
            ),
        },
    )

    assert git_ops._checkout_remote_matches("/repo", "acme", "widget") is False


def test_mismatched_fetch_url_fails_without_checking_push_url(monkeypatch) -> None:
    """A fetch URL naming a different repo fails immediately, without spending a
    second git call on the push URL it can no longer rescue."""
    calls = _patch_git(
        monkeypatch,
        {
            ("remote", "get-url", "origin"): (0, "https://github.com/evil/other.git"),
        },
    )

    assert git_ops._checkout_remote_matches("/repo", "acme", "widget") is False
    # Short-circuits on the fetch-URL mismatch; never bothers checking push.
    assert ("remote", "get-url", "--push", "--all", "origin") not in calls


def test_push_url_lookup_failure_fails_closed(monkeypatch) -> None:
    """A non-zero rc from the PUSH-url lookup leaves the push destinations
    unverifiable, which must be treated exactly like a mismatch (False), never
    assumed to match on the strength of the fetch URL alone."""
    _patch_git(
        monkeypatch,
        {
            ("remote", "get-url", "origin"): (0, "https://github.com/acme/widget.git"),
            ("remote", "get-url", "--push", "--all", "origin"): (
                1,
                "fatal: no such remote 'origin'",
            ),
        },
    )

    assert git_ops._checkout_remote_matches("/repo", "acme", "widget") is False


def test_fetch_url_lookup_failure_fails_closed(monkeypatch) -> None:
    """The FETCH-url counterpart: a non-zero rc (no `origin` remote, git missing,
    timeout -- `_git` degrades to a return code rather than raising) returns
    False and never reaches the push-URL lookup, since there is nothing left to
    validate against."""
    calls = _patch_git(
        monkeypatch,
        {("remote", "get-url", "origin"): (1, "fatal: no such remote 'origin'")},
    )

    assert git_ops._checkout_remote_matches("/repo", "acme", "widget") is False
    assert ("remote", "get-url", "--push", "--all", "origin") not in calls


def test_expected_host_is_threaded_through_to_both_checks(monkeypatch) -> None:
    """A GHES checkout (whose fetch/push URLs use the enterprise host, not
    github.com) validates when the caller passes its own `expected_host`."""
    _patch_git(
        monkeypatch,
        {
            ("remote", "get-url", "origin"): (0, "https://ghes.example.com/acme/widget.git"),
            ("remote", "get-url", "--push", "--all", "origin"): (
                0,
                "https://ghes.example.com/acme/widget.git",
            ),
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


def test_second_pushurl_pointing_elsewhere_fails(monkeypatch) -> None:
    """`--push` alone only reports the FIRST configured `pushurl`; a second,
    unrelated one (which `git push` also publishes to) must fail validation
    too — this is what `--all` (over bare `--push`) is for."""
    calls = _patch_git(
        monkeypatch,
        {
            ("remote", "get-url", "origin"): (0, "https://github.com/acme/widget.git"),
            ("remote", "get-url", "--push", "--all", "origin"): (
                0,
                "https://github.com/acme/widget.git\nhttps://github.com/evil/other.git",
            ),
        },
    )

    assert git_ops._checkout_remote_matches("/repo", "acme", "widget") is False
    assert ("remote", "get-url", "--push", "--all", "origin") in calls


def test_ssh_fetch_and_https_push_url_both_match(monkeypatch) -> None:
    """`_checkout_remote_matches` delegates to `remote_url_matches`, which is
    format-agnostic (scp-style SSH vs. HTTPS) as long as host/owner/repo agree
    -- so a fetch URL in one form and a push URL in the other, both naming the
    same repo, must still validate. This pins that real behavior rather than
    guessing it: every other test in this file happens to use byte-identical
    fetch/push URLs, which can't distinguish exact-string comparison from this
    owner/repo-aware comparison."""
    _patch_git(
        monkeypatch,
        {
            ("remote", "get-url", "origin"): (0, "git@github.com:acme/widget.git"),
            ("remote", "get-url", "--push", "--all", "origin"): (
                0,
                "https://github.com/acme/widget.git",
            ),
        },
    )

    assert git_ops._checkout_remote_matches("/repo", "acme", "widget") is True


def test_missing_git_suffix_on_push_url_still_matches(monkeypatch) -> None:
    """`remote_url_matches` strips a trailing `.git` before comparing, so a
    push URL configured without the suffix (some tooling omits it) must still
    validate against a fetch URL that has it."""
    _patch_git(
        monkeypatch,
        {
            ("remote", "get-url", "origin"): (0, "https://github.com/acme/widget.git"),
            ("remote", "get-url", "--push", "--all", "origin"): (
                0,
                "https://github.com/acme/widget",
            ),
        },
    )

    assert git_ops._checkout_remote_matches("/repo", "acme", "widget") is True


def test_multiple_matching_pushurls_pass(monkeypatch) -> None:
    """Multiple configured pushurls that ALL point at the expected repo (e.g.
    a redundant mirror entry) must still validate."""
    _patch_git(
        monkeypatch,
        {
            ("remote", "get-url", "origin"): (0, "https://github.com/acme/widget.git"),
            ("remote", "get-url", "--push", "--all", "origin"): (
                0,
                "https://github.com/acme/widget.git\nhttps://github.com/acme/widget.git",
            ),
        },
    )

    assert git_ops._checkout_remote_matches("/repo", "acme", "widget") is True
