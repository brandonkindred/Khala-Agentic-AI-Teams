"""start_coding_team_workflow dispatches CodingTeamWorkflow with the API's job_id.

The helper is a thin wrapper over start_workflow_sync; the contract that matters
is that it forwards the workflow run ref, a payload carrying the caller's job_id
(so the client polls the row the orchestrator writes), the deterministic
workflow id, and the coding-team task queue.
"""

from __future__ import annotations

import pytest

from software_engineering_team.temporal import coding_team_start_workflow as sw
from software_engineering_team.temporal.coding_team_constants import TASK_QUEUE, WORKFLOW_ID_PREFIX


@pytest.fixture(autouse=True)
def _stub_execute_workflow_sync(monkeypatch):
    """Safety net so the validation-only tests below never reach the real
    Temporal call after their expected ValueError. A test that needs to
    observe the call (e.g. to assert on its arguments) overrides this with
    its own monkeypatch.setattr, which simply takes precedence."""
    monkeypatch.setattr(sw, "execute_workflow_sync", lambda *a, **k: {"status": "completed"})


def test_start_coding_team_workflow_forwards_run_payload_id_and_queue(monkeypatch):
    captured: dict = {}

    def _fake_start_workflow_sync(workflow_run, *args, workflow_id, task_queue):
        captured["workflow_run"] = workflow_run
        captured["args"] = args
        captured["workflow_id"] = workflow_id
        captured["task_queue"] = task_queue

    monkeypatch.setattr(sw, "start_workflow_sync", _fake_start_workflow_sync)

    plan = {"objective": "ship it"}
    sw.start_coding_team_workflow("job-7", "/repo", plan)

    assert captured["workflow_run"] is sw.CodingTeamWorkflow.run
    (payload,) = captured["args"]
    assert payload == {"job_id": "job-7", "repo_path": "/repo", "plan_input": plan}
    assert captured["workflow_id"] == f"{WORKFLOW_ID_PREFIX}job-7"
    assert captured["workflow_id"] == "coding_team-job-7"
    assert captured["task_queue"] == TASK_QUEUE


def test_start_coding_team_workflow_requires_job_id(monkeypatch):
    monkeypatch.setattr(sw, "start_workflow_sync", lambda *a, **k: None)
    with pytest.raises(ValueError, match="non-empty job_id"):
        sw.start_coding_team_workflow("", "/repo", {"objective": "x"})


def test_start_coding_team_workflow_requires_repo_path(monkeypatch):
    monkeypatch.setattr(sw, "start_workflow_sync", lambda *a, **k: None)
    with pytest.raises(ValueError, match="non-empty repo_path"):
        sw.start_coding_team_workflow("job-7", "", {"objective": "x"})


def test_start_coding_team_workflow_includes_github_block(monkeypatch):
    captured: dict = {}

    def _fake_start_workflow_sync(workflow_run, *args, workflow_id, task_queue):
        captured["args"] = args

    monkeypatch.setattr(sw, "start_workflow_sync", _fake_start_workflow_sync)

    github = {
        "owner": "acme",
        "repo": "widgets",
        "issue_number": 9,
        "issue_title": "Fix it",
        "remote": "origin",
        "base": "main",
        "integration_branch": "khala/issue-9",
        "cleanup_checkout_on_success": False,
    }
    sw.start_coding_team_workflow("job-7", "/repo", {"objective": "x"}, github=github)

    (payload,) = captured["args"]
    assert payload["github"] == github
    assert "token" not in payload
    assert "token" not in payload["github"]


def test_start_coding_team_workflow_omits_github_when_none(monkeypatch):
    captured: dict = {}

    def _fake_start_workflow_sync(workflow_run, *args, workflow_id, task_queue):
        captured["args"] = args

    monkeypatch.setattr(sw, "start_workflow_sync", _fake_start_workflow_sync)

    sw.start_coding_team_workflow("job-7", "/repo", {"objective": "x"}, github=None)
    (payload,) = captured["args"]
    assert "github" not in payload


def test_execute_coding_team_workflow_waits_for_terminal_result(monkeypatch):
    captured: dict = {}

    def _fake_execute(workflow_run, *args, **kwargs):
        captured.update({"workflow_run": workflow_run, "args": args, **kwargs})
        return {"status": "completed", "github_pr_url": "https://example/pr/7"}

    monkeypatch.setattr(sw, "execute_workflow_sync", _fake_execute)
    github = {
        "owner": "acme",
        "repo": "widgets",
        "pr_number": 7,
        "publish_mode": "existing_pr",
        "base": "main",
        "integration_branch": "feature",
    }

    result = sw.execute_coding_team_workflow("parent:comment:2", "/repo", {"x": 1}, github)

    assert result["status"] == "completed"
    assert captured["workflow_id"] == "coding_team-parent:comment:2"
    assert captured["task_queue"] == TASK_QUEUE
    assert captured["args"][0]["github"] == github
    assert captured["execute_timeout_s"] == sw._COMMENT_WORKFLOW_TIMEOUT_S
    # A client-side timeout must never be mistaken for workflow failure: this
    # caller reattaches to the same still-running workflow rather than giving
    # up after one wait window (see runner.execute_workflow_sync's docstring).
    assert captured["reattach_on_timeout"] is True


# A github payload that passes validation on its own, so the two argument-presence
# tests below isolate the branch they name instead of silently depending on
# validation ORDER (an empty `{}` is itself rejected by `_validate_github_arg`, so
# either test would still pass with its own check removed).
_VALID_GITHUB = {"owner": "acme", "repo": "widgets", "pr_number": 7}


def test_execute_coding_team_workflow_requires_job_id(monkeypatch):
    with pytest.raises(ValueError, match="non-empty job_id"):
        sw.execute_coding_team_workflow("", "/repo", {"x": 1}, dict(_VALID_GITHUB))


def test_execute_coding_team_workflow_requires_repo_path(monkeypatch):
    with pytest.raises(ValueError, match="non-empty repo_path"):
        sw.execute_coding_team_workflow("job-7", "", {"x": 1}, dict(_VALID_GITHUB))


def test_execute_coding_team_workflow_rejects_plaintext_token(monkeypatch):
    # Preconditions must be enforced reliably (not via `assert`, which is
    # stripped under python -O) since this rejects a real secret-leak risk.
    with pytest.raises(ValueError, match="must not include a token"):
        sw.execute_coding_team_workflow("job-7", "/repo", {"x": 1}, {"token": "ghp_secret"})


def test_execute_coding_team_workflow_rejects_nested_plaintext_token(monkeypatch):
    """P1 regression: a token buried under a sub-dict (e.g. `github["auth"]
    ["token"]`) must be rejected too — a top-level-only check would let it
    slip into the durable Temporal event history."""
    with pytest.raises(ValueError, match="must not include a token"):
        sw.execute_coding_team_workflow(
            "job-7", "/repo", {"x": 1}, {"auth": {"token": "ghp_secret"}}
        )


def test_execute_coding_team_workflow_rejects_token_nested_in_list(monkeypatch):
    with pytest.raises(ValueError, match="must not include a token"):
        sw.execute_coding_team_workflow(
            "job-7", "/repo", {"x": 1}, {"extra": [{"token": "ghp_secret"}]}
        )


def test_execute_coding_team_workflow_rejects_non_dict_github(monkeypatch):
    """A caller passing None (or any non-dict) for github must get a clear
    ValueError, not a raw TypeError from `"token" in github`."""
    with pytest.raises(ValueError, match="non-empty github dict"):
        sw.execute_coding_team_workflow("job-7", "/repo", {"x": 1}, None)  # type: ignore[arg-type]


def test_execute_coding_team_workflow_rejects_empty_github_dict(monkeypatch):
    """An empty dict passes isinstance(github, dict) but carries no PR/comment
    context at all -- must fail fast rather than starting a durable workflow
    that can never reply to or resolve anything."""
    with pytest.raises(ValueError, match="non-empty github dict"):
        sw.execute_coding_team_workflow("job-7", "/repo", {"x": 1}, {})


@pytest.mark.parametrize(
    "dispatch",
    [
        lambda bad: sw.start_coding_team_workflow("job-7", "/repo", bad),
        lambda bad: sw.execute_coding_team_workflow("job-7", "/repo", bad, dict(_VALID_GITHUB)),
    ],
    ids=["start", "execute"],
)
def test_dispatchers_reject_non_dict_plan_input(monkeypatch, dispatch):
    """`plan_input` is serialized into the same durable payload as `github`, so a
    truthy NON-dict (a bare token string, say) must be rejected outright:
    `_contains_token_key` only traverses dicts/lists/tuples, so the token check
    alone returns False for it and the value would be written into Temporal
    history verbatim -- the identical hole `github` already guards against."""
    monkeypatch.setattr(sw, "start_workflow_sync", lambda *a, **k: None)
    with pytest.raises(ValueError, match="requires plan_input to be a dict when provided"):
        dispatch("ghp_secret")


def test_start_coding_team_workflow_rejects_token_in_plan_input(monkeypatch):
    """A dict `plan_input` is still token-scanned at any nesting depth."""
    monkeypatch.setattr(sw, "start_workflow_sync", lambda *a, **k: None)
    with pytest.raises(ValueError, match="plan_input to not include a token"):
        sw.start_coding_team_workflow("job-7", "/repo", {"auth": {"token": "ghp_secret"}})


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "GITHUB_TOKEN",
        "authorization",
        "Authorization",
        "api_key",
        "API-KEY",
        # Unseparated spellings: substring matching is literal, so "apikey"
        # contains neither "api_key" nor "api-key" and needs its own marker.
        "apikey",
        "APIKey",
        "x-apikey",
        "client_secret",
        "password",
        # "passphrase" does NOT contain "password" -- a separate marker.
        "passphrase",
        "ssh_passphrase",
        # GitHub App signing keys / SSH PEMs.
        "private_key",
        "PRIVATE_KEY",
        "app_private_key",
        "credentials",
    ],
)
def test_contains_token_key_flags_every_credential_marker(key):
    """Defense in depth: a credential smuggled under any of the common key
    spellings -- not just "token" -- must be caught before it reaches Temporal's
    permanent event history."""
    assert sw._contains_token_key({key: "value"}) is True
    assert sw._contains_token_key({"outer": [{key: "value"}]}) is True


@pytest.mark.parametrize(
    "key",
    [
        "owner",
        "repo",
        "issue_number",
        "issue_title",
        "remote",
        "base",
        "integration_branch",
        "expected_base_sha",
        "expected_head_sha",
        "pr_number",
        "pr_url",
        "publish_mode",
        "cleanup_checkout_on_success",
        "requirements_title",
        "requirements_description",
        "project_overview",
        "completed_work_summary",
        "repo_path",
    ],
)
def test_contains_token_key_does_not_flag_real_payload_keys(key):
    """Every key the real `github`/`plan_input` payloads actually carry must stay
    dispatchable -- the broadened marker list must not false-positive on them."""
    assert sw._contains_token_key({key: "value"}) is False


def test_contains_token_key_terminates_on_a_reference_cycle():
    """A self-referential payload must not recurse forever: the visited set is
    identity-keyed, so the cycle is closed on the second encounter."""
    cyclic: dict = {"a": 1}
    cyclic["self"] = cyclic
    assert sw._contains_token_key(cyclic) is False

    cyclic_with_token: dict = {"token": "ghp_secret"}
    cyclic_with_token["self"] = cyclic_with_token
    assert sw._contains_token_key(cyclic_with_token) is True


def test_contains_token_key_visits_a_shared_substructure_once():
    """The visited set accumulates across the WHOLE traversal, not per path: a
    diamond-shaped payload (one shared child referenced from many parents) is
    walked once per container, not once per reference -- otherwise k levels of
    sharing cost O(2**k)."""
    shared: dict = {"leaf": "v"}
    for _ in range(30):
        shared = {"l": shared, "r": shared}
    # With a per-path (copied) visited set this is 2**30 traversals and would
    # never return; with one accumulated set it is linear in container count.
    assert sw._contains_token_key(shared) is False


def test_contains_token_key_still_finds_a_token_inside_a_shared_substructure():
    """Deduplicating shared containers must not lose a real finding: the FIRST
    visit still walks the shared child in full."""
    shared = {"nested": {"github_token": "ghp_secret"}}
    diamond = {"l": shared, "r": shared}
    assert sw._contains_token_key(diamond) is True
