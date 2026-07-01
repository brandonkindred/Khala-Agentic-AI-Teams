"""Tests for the GitHub webhook handler (``@khala review`` PR-comment trigger)."""

import hashlib
import hmac
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from unified_api import github_events_handler as gh  # noqa: E402

_H = "unified_api.github_events_handler"


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature_accepts_valid():
    body = b'{"action":"created"}'
    assert gh.verify_github_signature("topsecret", body, _sign("topsecret", body)) is True


def test_verify_signature_rejects_wrong_secret():
    body = b'{"action":"created"}'
    assert gh.verify_github_signature("topsecret", body, _sign("other", body)) is False


def test_verify_signature_rejects_tampered_body():
    body = b'{"action":"created"}'
    sig = _sign("topsecret", body)
    assert gh.verify_github_signature("topsecret", b'{"action":"deleted"}', sig) is False


def test_verify_signature_rejects_wrong_scheme():
    body = b"x"
    digest = hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
    assert gh.verify_github_signature("topsecret", body, f"sha1={digest}") is False


def test_verify_signature_rejects_missing_header():
    assert gh.verify_github_signature("topsecret", b"x", "") is False


def test_verify_signature_rejects_empty_secret():
    body = b"x"
    assert gh.verify_github_signature("", body, _sign("topsecret", body)) is False


def test_verify_signature_rejects_malformed_header_no_equals():
    assert gh.verify_github_signature("topsecret", b"x", "sha256") is False


# ---------------------------------------------------------------------------
# Command parsing + authorization
# ---------------------------------------------------------------------------


def test_parse_review_command_matches_basic():
    assert gh.parse_review_command("@khala review") is True


def test_parse_review_command_matches_within_text():
    assert gh.parse_review_command("Hey @khala review this please") is True


def test_parse_review_command_case_insensitive_and_extra_space():
    assert gh.parse_review_command("@Khala    REVIEW") is True


def test_parse_review_command_ignores_other_commands():
    assert gh.parse_review_command("@khala deploy") is False


def test_parse_review_command_ignores_plain_text():
    assert gh.parse_review_command("please review this") is False


def test_parse_review_command_empty():
    assert gh.parse_review_command("") is False


def test_parse_review_command_requires_word_boundary():
    # "reviewer" must not satisfy the command (word boundary after "review").
    assert gh.parse_review_command("@khala reviewer") is False


def test_is_authorized_allows_collaborator_roles():
    for role in ("OWNER", "MEMBER", "COLLABORATOR", "collaborator"):
        assert gh.is_authorized(role) is True


def test_is_authorized_rejects_outside_contributor():
    for role in ("CONTRIBUTOR", "FIRST_TIMER", "NONE", "", None):
        assert gh.is_authorized(role) is False


# ---------------------------------------------------------------------------
# dispatch_github_event
# ---------------------------------------------------------------------------


def _comment_payload(**over):
    payload = {
        "action": "created",
        "issue": {"number": 42, "pull_request": {"url": "https://api.github.com/.../pulls/42"}},
        "comment": {
            "id": 999,
            "body": "@khala review",
            "author_association": "COLLABORATOR",
            "user": {"type": "User"},
        },
        "repository": {"name": "widget", "owner": {"login": "acme"}},
    }
    payload.update(over)
    return payload


class _SyncExecutor:
    """Executor stand-in that runs submitted work synchronously (no thread pool)."""

    def submit(self, fn, *args):
        fn(*args)


def _dispatch(payload, event_type="issue_comment", cfg=("acme", "widget")):
    """Run dispatch with process_review_request + the dispatch executor patched; return the mock."""
    proc = MagicMock()
    with (
        patch(f"{_H}.process_review_request", proc),
        patch(f"{_H}._get_dispatch_executor", return_value=_SyncExecutor()),
        patch(f"{_H}._configured_owner_repo", return_value=cfg),
    ):
        gh.dispatch_github_event(event_type, payload)
    return proc


def test_dispatch_triggers_review_on_valid_comment():
    proc = _dispatch(_comment_payload())
    proc.assert_called_once_with("acme", "widget", 42, 999)


def test_dispatch_ignores_non_issue_comment_event():
    assert _dispatch(_comment_payload(), event_type="push").called is False


def test_dispatch_ignores_non_created_action():
    assert _dispatch(_comment_payload(action="edited")).called is False


def test_dispatch_ignores_comment_on_plain_issue():
    payload = _comment_payload(issue={"number": 42})  # no pull_request key
    assert _dispatch(payload).called is False


def test_dispatch_ignores_bot_comment():
    payload = _comment_payload()
    payload["comment"]["user"] = {"type": "Bot"}
    assert _dispatch(payload).called is False


def test_dispatch_ignores_non_command_comment():
    payload = _comment_payload()
    payload["comment"]["body"] = "looks good to me"
    assert _dispatch(payload).called is False


def test_dispatch_ignores_unauthorized_association():
    payload = _comment_payload()
    payload["comment"]["author_association"] = "CONTRIBUTOR"
    assert _dispatch(payload).called is False


def test_dispatch_ignores_repo_mismatch():
    # Comment on acme/widget but configured repo is acme/other → ignored.
    assert _dispatch(_comment_payload(), cfg=("acme", "other")).called is False


def test_dispatch_repo_match_is_case_insensitive():
    payload = _comment_payload(repository={"name": "Widget", "owner": {"login": "ACME"}})
    proc = _dispatch(payload, cfg=("acme", "widget"))
    proc.assert_called_once()


def test_dispatch_ignores_when_no_configured_repo():
    assert _dispatch(_comment_payload(), cfg=("", "")).called is False


def test_dispatch_ignores_non_integer_pr_number():
    payload = _comment_payload()
    payload["issue"]["number"] = "not-an-int"
    assert _dispatch(payload).called is False


def test_dispatch_defaults_missing_comment_id_to_zero():
    payload = _comment_payload()
    del payload["comment"]["id"]
    proc = _dispatch(payload)
    proc.assert_called_once_with("acme", "widget", 42, 0)


# ---------------------------------------------------------------------------
# process_review_request / reaction / review start
# ---------------------------------------------------------------------------


def test_process_review_request_resolves_token_once_then_reacts_and_starts():
    # The token is resolved a SINGLE time and reused by both the reaction and the
    # review start (no duplicate credential-store read per webhook).
    with (
        patch(f"{_H}._resolve_github_token", return_value="ghp_tok") as tok,
        patch(f"{_H}._add_eyes_reaction") as react,
        patch(f"{_H}._start_review") as start,
    ):
        gh.process_review_request("acme", "widget", 42, 999)
    tok.assert_called_once_with()
    react.assert_called_once_with("acme", "widget", 999, "ghp_tok")
    start.assert_called_once_with(42, "ghp_tok")


def test_add_eyes_reaction_noop_without_comment_id():
    fake_client = MagicMock()
    with patch("coding_team.github_source.client.GitHubClient", return_value=fake_client):
        gh._add_eyes_reaction("acme", "widget", 0, "ghp_tok")
    fake_client.create_comment_reaction.assert_not_called()


def test_add_eyes_reaction_noop_without_token():
    fake_client = MagicMock()
    with patch("coding_team.github_source.client.GitHubClient", return_value=fake_client):
        # No token → no client built, must not raise.
        gh._add_eyes_reaction("acme", "widget", 999, None)
    fake_client.create_comment_reaction.assert_not_called()


def test_add_eyes_reaction_calls_client():
    fake_client = MagicMock()
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    with patch("coding_team.github_source.client.GitHubClient", return_value=fake_client):
        gh._add_eyes_reaction("acme", "widget", 999, "ghp_tok")
    fake_client.create_comment_reaction.assert_called_once_with("acme", "widget", 999, content="eyes")


def test_add_eyes_reaction_swallows_errors():
    with patch("coding_team.github_source.client.GitHubClient", side_effect=RuntimeError("boom")):
        # Best-effort: an exception here must not propagate.
        gh._add_eyes_reaction("acme", "widget", 999, "ghp_tok")


def test_resolve_github_token_reads_credential():
    with patch("unified_api.integration_credentials.get_credential_status", return_value=("ghp_tok", True)):
        assert gh._resolve_github_token() == "ghp_tok"


def test_resolve_github_token_none_when_absent():
    with patch("unified_api.integration_credentials.get_credential_status", return_value=("", True)):
        assert gh._resolve_github_token() is None


def test_resolve_github_token_none_on_error():
    with patch("unified_api.integration_credentials.get_credential_status", side_effect=RuntimeError("db down")):
        assert gh._resolve_github_token() is None


def test_configured_owner_repo_empty_on_error():
    with patch("unified_api.integrations_store.get_github_config_meta", side_effect=RuntimeError("db down")):
        assert gh._configured_owner_repo() == ("", "")


def test_start_review_invokes_shared_helper_with_token():
    seen = {}

    async def _fake_start(pr_number, base_branch, *, token=None):
        seen["args"] = (pr_number, base_branch, token)
        return MagicMock(job_id="job-1")

    with patch("unified_api.routes.integrations._start_pr_review", _fake_start):
        gh._start_review(42, "ghp_tok")  # must not raise
    # The pre-resolved token is threaded through so the review path does not re-read it.
    assert seen["args"] == (42, None, "ghp_tok")


def test_start_review_swallows_errors():
    async def _boom(pr_number, base_branch, *, token=None):
        raise RuntimeError("downstream down")

    with patch("unified_api.routes.integrations._start_pr_review", _boom):
        gh._start_review(42, "ghp_tok")  # logged, not raised


def test_configured_owner_repo_reads_meta():
    with patch(
        "unified_api.integrations_store.get_github_config_meta",
        return_value={"enabled": True, "owner": "acme", "repo": "widget"},
    ):
        assert gh._configured_owner_repo() == ("acme", "widget")


def test_configured_owner_repo_empty_when_disabled():
    """A disabled integration reports as unconfigured even with owner/repo/PAT still
    saved from before it was turned off — the webhook must not treat a disabled
    integration as a valid review target."""
    with patch(
        "unified_api.integrations_store.get_github_config_meta",
        return_value={"enabled": False, "owner": "acme", "repo": "widget"},
    ):
        assert gh._configured_owner_repo() == ("", "")


# ---------------------------------------------------------------------------
# _get_dispatch_executor: bounded pool, lazily created and reused
# ---------------------------------------------------------------------------


def test_get_dispatch_executor_reuses_same_instance():
    gh._DISPATCH_EXECUTOR = None
    try:
        first = gh._get_dispatch_executor()
        second = gh._get_dispatch_executor()
        assert first is second
    finally:
        first.shutdown(wait=False, cancel_futures=True)
        gh._DISPATCH_EXECUTOR = None


def test_get_dispatch_executor_recreates_after_shutdown():
    gh._DISPATCH_EXECUTOR = None
    try:
        first = gh._get_dispatch_executor()
        first.shutdown(wait=False, cancel_futures=True)
        second = gh._get_dispatch_executor()
        assert second is not first
    finally:
        gh._get_dispatch_executor().shutdown(wait=False, cancel_futures=True)
        gh._DISPATCH_EXECUTOR = None


def test_dispatch_submits_to_bounded_executor_not_raw_thread():
    """dispatch_github_event submits via the bounded executor, not an ad-hoc thread."""
    fake_executor = MagicMock()
    with (
        patch(f"{_H}._get_dispatch_executor", return_value=fake_executor),
        patch(f"{_H}._configured_owner_repo", return_value=("acme", "widget")),
        patch(f"{_H}.process_review_request") as proc,
    ):
        gh.dispatch_github_event("issue_comment", _comment_payload())
    fake_executor.submit.assert_called_once_with(proc, "acme", "widget", 42, 999)
