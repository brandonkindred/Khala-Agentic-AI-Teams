"""Tests for the GitHub webhook handler (``@khala review`` PR-comment trigger)."""

import hashlib
import hmac
import sys
from concurrent import futures
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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


def test_verify_signature_rejects_non_ascii_header_without_raising():
    # Starlette decodes header bytes as latin-1, so a byte >= 0x80 becomes a non-ASCII
    # str char. Comparing digests as str would make hmac.compare_digest raise TypeError
    # (→ unauthenticated 500); comparing as bytes must return False and never raise.
    body = b"x"
    assert gh.verify_github_signature("topsecret", body, "sha256=deadbeef\xff") is False


def test_verify_signature_valid_still_matches_after_bytes_fix():
    body = b'{"a":1}'
    assert gh.verify_github_signature("topsecret", body, _sign("topsecret", body)) is True


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
            "user": {"type": "User", "login": "alice"},
        },
        "repository": {"name": "widget", "owner": {"login": "acme"}},
    }
    payload.update(over)
    return payload


class _SyncExecutor:
    """Executor stand-in that runs submitted work synchronously (no thread pool).

    Returns a resolved ``Future`` so ``submit_safely``'s ``add_done_callback`` works.
    """

    def submit(self, fn, *args):
        fut: futures.Future = futures.Future()
        try:
            fut.set_result(fn(*args))
        except BaseException as e:  # noqa: BLE001 - mirror to the future like a real pool
            fut.set_exception(e)
        return fut


def _dispatch(payload, event_type="issue_comment", delivery_id=""):
    """Run dispatch with process_review_request + the dispatch executor patched; return the mock.

    dispatch_github_event performs only pure payload checks — the configured-repo match
    happens later, on the worker, inside process_review_request (so the config file read
    never blocks the event loop).
    """
    proc = MagicMock()
    with (
        patch(f"{_H}.process_review_request", proc),
        patch(f"{_H}._get_dispatch_executor", return_value=_SyncExecutor()),
    ):
        gh.dispatch_github_event(event_type, payload, delivery_id)
    return proc


def test_dispatch_triggers_review_on_valid_comment():
    proc = _dispatch(_comment_payload())
    proc.assert_called_once_with("acme", "widget", 42, 999, "alice", "")


def test_dispatch_ignores_non_issue_comment_event():
    assert _dispatch(_comment_payload(), event_type="push").called is False


def test_dispatch_ignores_non_created_action():
    assert _dispatch(_comment_payload(action="edited")).called is False


@pytest.mark.parametrize("bad_payload", [[], 5, "x", None, True])
def test_dispatch_ignores_non_dict_payload_without_raising(bad_payload):
    # A non-object JSON body must be a silent no-op (dispatch's "never raises" contract),
    # not an AttributeError from payload.get(...).
    assert _dispatch(bad_payload, delivery_id="d-1").called is False


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


def test_dispatch_forwards_payload_repo_for_worker_side_config_match():
    # The configured-repo comparison happens on the worker (see the worker-level
    # repo-match tests below); dispatch just forwards the payload's coordinates.
    payload = _comment_payload(repository={"name": "Widget", "owner": {"login": "ACME"}})
    proc = _dispatch(payload)
    proc.assert_called_once_with("ACME", "Widget", 42, 999, "alice", "")


@pytest.mark.parametrize("bad_number", ["not-an-int", None, True, False, 0, -7])
def test_dispatch_rejects_invalid_pr_numbers(bad_number):
    # bool is an int subclass and PR numbers are strictly positive; both must be
    # rejected here so process_review_request's "positive PR number" precondition holds.
    payload = _comment_payload()
    payload["issue"]["number"] = bad_number
    assert _dispatch(payload).called is False


def test_dispatch_defaults_missing_comment_id_to_zero():
    payload = _comment_payload()
    del payload["comment"]["id"]
    proc = _dispatch(payload)
    proc.assert_called_once_with("acme", "widget", 42, 0, "alice", "")


def test_dispatch_forgets_delivery_when_submit_dropped():
    """If the executor rejects the work (process teardown), the delivery must be forgotten
    again — otherwise a redelivery of never-run work is swallowed for the whole TTL."""
    gh._SEEN_DELIVERY_IDS.clear()
    with (
        patch(f"{_H}.process_review_request", MagicMock()),
        patch(f"{_H}._get_dispatch_executor", return_value=_SyncExecutor()),
        patch(f"{_H}.submit_safely", return_value=False),
    ):
        gh.dispatch_github_event("issue_comment", _comment_payload(), "d-dropped")
    assert "d-dropped" not in gh._SEEN_DELIVERY_IDS


# ---------------------------------------------------------------------------
# process_review_request / reaction / review start
# ---------------------------------------------------------------------------


def _process(owner="acme", repo="widget", cfg=("acme", "widget"), start="started", own_login=""):
    """Run process_review_request with its collaborators patched; return the mock bundle."""
    mocks = {
        "tok": patch(f"{_H}._resolve_github_token", return_value="ghp_tok"),
        "cfg": patch(f"{_H}._configured_owner_repo", return_value=cfg),
        "own": patch(f"{_H}._own_github_login", return_value=own_login),
        "start": patch(f"{_H}._start_review", return_value=start),
        "react": patch(f"{_H}._add_comment_reaction"),
        "forget": patch(f"{_H}._forget_delivery"),
    }
    started = {k: m.start() for k, m in mocks.items()}
    try:
        gh.process_review_request(owner, repo, 42, 999, "alice", "d-1")
    finally:
        for m in mocks.values():
            m.stop()
    return started


def test_process_review_request_resolves_token_once_starts_then_reacts_on_success():
    # The token is resolved a SINGLE time and reused for the self-comment check, the
    # review start, and the reaction; 👀 is posted only once the review is in flight.
    m = _process()
    m["tok"].assert_called_once_with()
    m["start"].assert_called_once_with("acme", "widget", 42, "ghp_tok")
    m["react"].assert_called_once_with("acme", "widget", 999, "ghp_tok", content="eyes")
    m["forget"].assert_not_called()


def test_process_review_request_acknowledges_already_running_review():
    # A duplicate request while a review is in flight is the intended outcome: the
    # commenter still gets the 👀 acknowledgement (a review IS running for this PR),
    # and the delivery stays marked seen (nothing to redo on redelivery).
    m = _process(start="already_running")
    m["react"].assert_called_once_with("acme", "widget", 999, "ghp_tok", content="eyes")
    m["forget"].assert_not_called()


def test_process_review_request_signals_failure_and_forgets_delivery():
    # Seen-but-could-not-run: the commenter gets a visible 😕 signal instead of silence,
    # and the delivery is forgotten so GitHub's "Redeliver" can retry it.
    m = _process(start="failed")
    m["react"].assert_called_once_with("acme", "widget", 999, "ghp_tok", content="confused")
    m["forget"].assert_called_once_with("d-1")


def test_process_review_request_ignores_repo_mismatch_and_forgets_delivery():
    # Comment on acme/widget but configured repo is acme/other → no review, and the
    # delivery is forgotten so a redelivery after a config fix is not swallowed.
    m = _process(cfg=("acme", "other"))
    m["start"].assert_not_called()
    m["react"].assert_not_called()
    m["forget"].assert_called_once_with("d-1")


def test_process_review_request_repo_match_is_case_insensitive():
    m = _process(owner="ACME", repo="Widget", cfg=("acme", "widget"))
    m["start"].assert_called_once_with("acme", "widget", 42, "ghp_tok")


def test_process_review_request_ignores_when_no_configured_repo():
    m = _process(cfg=("", ""))
    m["start"].assert_not_called()
    m["forget"].assert_called_once_with("d-1")


def test_process_review_request_skips_own_comment():
    """A comment posted by the PAT's own account (type "User", OWNER-associated) passes
    the Bot check — the own-login match must stop it from re-triggering a review loop."""
    m = _process(own_login="alice")  # comment author is "alice" (payload default)
    m["start"].assert_not_called()
    m["react"].assert_not_called()
    m["forget"].assert_not_called()  # nothing to redeliver — the skip is final


def test_process_review_request_own_login_match_is_case_insensitive():
    m = _process(own_login="ALICE")
    m["start"].assert_not_called()


def test_process_review_request_proceeds_when_own_login_unknown():
    # "" means the lookup failed — fail open (an API blip must not block real requests).
    m = _process(own_login="")
    m["start"].assert_called_once()


def test_add_comment_reaction_noop_without_comment_id():
    fake_client = MagicMock()
    with patch("coding_team.github_source.client.GitHubClient", return_value=fake_client):
        gh._add_comment_reaction("acme", "widget", 0, "ghp_tok")
    fake_client.create_comment_reaction.assert_not_called()


def test_add_comment_reaction_noop_without_token():
    fake_client = MagicMock()
    with patch("coding_team.github_source.client.GitHubClient", return_value=fake_client):
        # No token → no client built, must not raise.
        gh._add_comment_reaction("acme", "widget", 999, None)
    fake_client.create_comment_reaction.assert_not_called()


def test_add_comment_reaction_calls_client_with_default_eyes():
    fake_client = MagicMock()
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    with patch("coding_team.github_source.client.GitHubClient", return_value=fake_client):
        gh._add_comment_reaction("acme", "widget", 999, "ghp_tok")
    fake_client.create_comment_reaction.assert_called_once_with("acme", "widget", 999, content="eyes")


def test_add_comment_reaction_passes_custom_content():
    fake_client = MagicMock()
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    with patch("coding_team.github_source.client.GitHubClient", return_value=fake_client):
        gh._add_comment_reaction("acme", "widget", 999, "ghp_tok", content="confused")
    fake_client.create_comment_reaction.assert_called_once_with("acme", "widget", 999, content="confused")


def test_add_comment_reaction_swallows_errors():
    with patch("coding_team.github_source.client.GitHubClient", side_effect=RuntimeError("boom")):
        # Best-effort: an exception here must not propagate.
        gh._add_comment_reaction("acme", "widget", 999, "ghp_tok")


def test_own_github_login_resolves_and_caches():
    gh._OWN_LOGIN_CACHE.clear()
    fake_client = MagicMock()
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    fake_client.get_authenticated_login.return_value = "khala-bot"
    with patch("coding_team.github_source.client.GitHubClient", return_value=fake_client) as cls:
        assert gh._own_github_login("ghp_tok") == "khala-bot"
        assert gh._own_github_login("ghp_tok") == "khala-bot"  # served from cache
    assert cls.call_count == 1


def test_own_github_login_empty_without_token_and_on_error():
    gh._OWN_LOGIN_CACHE.clear()
    assert gh._own_github_login(None) == ""
    assert gh._own_github_login("") == ""
    with patch("coding_team.github_source.client.GitHubClient", side_effect=RuntimeError("api down")):
        assert gh._own_github_login("ghp_tok") == ""  # fail-open, logged, not raised


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


def test_start_review_invokes_shared_helper_with_token_and_returns_started():
    seen = {}

    async def _fake_start(pr_number, base_branch, *, token=None, expected_owner=None, expected_repo=None):
        seen["args"] = (pr_number, base_branch, token, expected_owner, expected_repo)
        return MagicMock(job_id="job-1")

    with patch("unified_api.routes.integrations._start_pr_review", _fake_start):
        assert gh._start_review("acme", "widget", 42, "ghp_tok") == "started"
    # The pre-resolved token is threaded through so the review path does not re-read it,
    # and the validated owner/repo are passed as the expected target so a config change
    # mid-flight cannot redirect the review to a different repo.
    assert seen["args"] == (42, None, "ghp_tok", "acme", "widget")


def test_start_review_swallows_errors_and_returns_failed():
    async def _boom(pr_number, base_branch, *, token=None, expected_owner=None, expected_repo=None):
        raise RuntimeError("downstream down")

    with patch("unified_api.routes.integrations._start_pr_review", _boom):
        assert gh._start_review("acme", "widget", 42, "ghp_tok") == "failed"  # logged, not raised


def test_start_review_recognizes_already_running_409(caplog):
    """The duplicate-guard 409 is the intended outcome, not an error: it must map to
    'already_running' and log at info — never an error-level stack trace that reads as
    a broken webhook to operators."""
    from fastapi import HTTPException

    async def _dup(pr_number, base_branch, *, token=None, expected_owner=None, expected_repo=None):
        raise HTTPException(status_code=409, detail="review job abc already running for acme/widget#42")

    with patch("unified_api.routes.integrations._start_pr_review", _dup), caplog.at_level("INFO"):
        assert gh._start_review("acme", "widget", 42, "ghp_tok") == "already_running"
    assert not any(r.levelname == "ERROR" for r in caplog.records)


def test_start_review_treats_stale_config_409_as_failed():
    """The stale-config 409 (configured repo changed mid-flight) is NOT 'already
    running' — it must map to 'failed' so the commenter gets the 😕 signal and the
    delivery is forgotten for redelivery."""
    from fastapi import HTTPException

    async def _stale(pr_number, base_branch, *, token=None, expected_owner=None, expected_repo=None):
        raise HTTPException(status_code=409, detail="Configured repository changed since the review was requested")

    with patch("unified_api.routes.integrations._start_pr_review", _stale):
        assert gh._start_review("acme", "widget", 42, "ghp_tok") == "failed"


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
        patch(f"{_H}.process_review_request") as proc,
    ):
        gh.dispatch_github_event("issue_comment", _comment_payload())
    fake_executor.submit.assert_called_once_with(proc, "acme", "widget", 42, 999, "alice", "")


def test_dispatch_never_raises_when_executor_is_shut_down():
    """A shut-down dispatch executor (or an interpreter-shutdown race) must not make
    dispatch_github_event raise — its documented contract is 'never raises', and the
    webhook route has already returned its HTTP response by the time this runs."""
    real_executor = futures.ThreadPoolExecutor(max_workers=1)
    real_executor.shutdown(wait=True)
    with (
        patch(f"{_H}._get_dispatch_executor", return_value=real_executor),
        patch(f"{_H}._configured_owner_repo", return_value=("acme", "widget")),
        patch(f"{_H}.process_review_request") as proc,
    ):
        gh.dispatch_github_event("issue_comment", _comment_payload())  # must not raise
    proc.assert_not_called()


# ---------------------------------------------------------------------------
# _is_duplicate_delivery / redelivery dedup
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_seen_deliveries():
    """Reset the module-level dedup table so tests can't leak state into each other."""
    gh._SEEN_DELIVERY_IDS.clear()
    yield
    gh._SEEN_DELIVERY_IDS.clear()


def test_is_duplicate_delivery_empty_id_never_duplicate():
    assert gh._is_duplicate_delivery("") is False
    assert gh._is_duplicate_delivery("") is False
    assert gh._SEEN_DELIVERY_IDS == {}


def test_is_duplicate_delivery_first_seen_then_duplicate():
    assert gh._is_duplicate_delivery("d-1") is False
    assert gh._is_duplicate_delivery("d-1") is True


def test_is_duplicate_delivery_distinct_ids_independent():
    assert gh._is_duplicate_delivery("d-1") is False
    assert gh._is_duplicate_delivery("d-2") is False
    assert gh._is_duplicate_delivery("d-1") is True
    assert gh._is_duplicate_delivery("d-2") is True


def test_is_duplicate_delivery_expires_after_ttl():
    with patch(f"{_H}.time.monotonic", return_value=1000.0):
        assert gh._is_duplicate_delivery("d-1") is False
    with patch(f"{_H}.time.monotonic", return_value=1000.0 + gh._SEEN_DELIVERY_TTL_S + 1):
        # TTL elapsed: pruned and treated as a fresh delivery, not a duplicate.
        assert gh._is_duplicate_delivery("d-1") is False


def test_is_duplicate_delivery_caps_table_size():
    for i in range(gh._SEEN_DELIVERY_MAX_ENTRIES + 10):
        gh._is_duplicate_delivery(f"d-{i}")
    assert len(gh._SEEN_DELIVERY_IDS) <= gh._SEEN_DELIVERY_MAX_ENTRIES


def test_dispatch_ignores_redelivered_delivery_id():
    """The same delivery_id twice must only submit review work once."""
    proc = _dispatch(_comment_payload(), delivery_id="delivery-1")
    proc.assert_called_once_with("acme", "widget", 42, 999, "alice", "delivery-1")
    assert _dispatch(_comment_payload(), delivery_id="delivery-1").called is False


def test_dispatch_distinct_delivery_ids_both_processed():
    assert _dispatch(_comment_payload(), delivery_id="delivery-a").called is True
    assert _dispatch(_comment_payload(), delivery_id="delivery-b").called is True


def test_dispatch_without_delivery_id_never_deduped():
    """No X-GitHub-Delivery header (delivery_id="") must not suppress repeated triggers —
    only a real, matching delivery ID counts as a redelivery."""
    assert _dispatch(_comment_payload()).called is True
    assert _dispatch(_comment_payload()).called is True


def test_forget_delivery_allows_redelivery_to_retry():
    """A forgotten delivery (failed review start) must be dispatchable again — this is
    what makes GitHub's 'Redeliver' button work for a review that never actually ran."""
    gh._SEEN_DELIVERY_IDS.clear()
    assert _dispatch(_comment_payload(), delivery_id="d-retry").called is True
    assert _dispatch(_comment_payload(), delivery_id="d-retry").called is False  # deduped
    gh._forget_delivery("d-retry")
    assert _dispatch(_comment_payload(), delivery_id="d-retry").called is True  # retried


def test_forget_delivery_noop_for_empty_and_unknown_ids():
    gh._forget_delivery("")  # must not raise
    gh._forget_delivery("never-seen")  # must not raise
