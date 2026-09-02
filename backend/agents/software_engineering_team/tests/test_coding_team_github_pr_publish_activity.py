"""Unit tests for github_pr_publish_activity and the expected_head_sha passthrough
on github_branch_prep_activity — both added by the PR-comments-remediation flow
and previously untested (see the module's own docstring contracts)."""

from __future__ import annotations

import os
from typing import Any

import pytest

from software_engineering_team.models import JobStatus
from software_engineering_team.tests.conftest import _ensure_real_modules, _stub_orchestrator_only


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Any:
    _ensure_real_modules()
    _stub_orchestrator_only(monkeypatch)
    from software_engineering_team.api import coding_team_main as api_main

    return api_main


def _pr_publish():
    from software_engineering_team.temporal.coding_team_github_activities import (
        github_pr_publish_activity,
    )

    return github_pr_publish_activity


def _branch_prep():
    from software_engineering_team.temporal.coding_team_github_activities import (
        github_branch_prep_activity,
    )

    return github_branch_prep_activity


def _base_request(**overrides: Any) -> dict[str, Any]:
    req = {
        "job_id": "job-1",
        "repo_path": "/repo",
        "pr_number": 7,
        "integration_branch": "khala/pr-7",
    }
    req.update(overrides)
    return req


def _stub_token(monkeypatch: pytest.MonkeyPatch, api: Any) -> None:
    monkeypatch.setattr(api, "get_job", lambda job_id, cache_dir=None: {"job_id": job_id})
    monkeypatch.setenv("GITHUB_TOKEN", "env-pat")
    monkeypatch.delenv("INTEGRATION_ENCRYPTION_KEY", raising=False)
    assert os.environ["GITHUB_TOKEN"] == "env-pat"


class TestGithubPrPublishActivityValidation:
    def test_missing_required_field_raises(self, monkeypatch: pytest.MonkeyPatch, api: Any) -> None:
        """An ABSENT (None) pr_number still falls through to the required-fields
        sweep, which names it as missing -- only a present-but-invalid value is
        diverted to the positive-integer check."""
        _stub_token(monkeypatch, api)
        with pytest.raises(ValueError, match="missing required fields"):
            _pr_publish()(_base_request(pr_number=None))

    @pytest.mark.parametrize("bad_pr_number", ["7", True, -1])
    def test_invalid_pr_number_raises(
        self, monkeypatch: pytest.MonkeyPatch, api: Any, bad_pr_number: Any
    ) -> None:
        _stub_token(monkeypatch, api)
        with pytest.raises(ValueError, match="positive integer pr_number"):
            _pr_publish()(_base_request(pr_number=bad_pr_number))

    def test_zero_pr_number_rejected_as_non_positive_not_as_missing(
        self, monkeypatch: pytest.MonkeyPatch, api: Any
    ) -> None:
        """``0`` is PRESENT but falsy: the truthiness-based required-fields sweep
        would misreport it as a MISSING field, so the positive-integer check runs
        first and rejects it with the precise message naming the bad value."""
        _stub_token(monkeypatch, api)
        with pytest.raises(ValueError, match="positive integer pr_number, got 0"):
            _pr_publish()(_base_request(pr_number=0))


class TestGithubPrPublishActivityGitFailures:
    def test_fast_forward_failure_raises_and_skips_update(
        self, monkeypatch: pytest.MonkeyPatch, api: Any
    ) -> None:
        _stub_token(monkeypatch, api)
        monkeypatch.setattr(api, "_fast_forward", lambda *a, **kw: (False, "not a fast-forward"))
        push_calls: list[tuple[Any, ...]] = []

        def _fake_push(*a: Any, **_kw: Any) -> tuple[bool, None]:
            """Record the positional args and report a successful push.

            Spelled as a named function rather than an
            ``append(a) or (True, None)`` lambda: that idiom only works because
            ``list.append`` happens to return ``None``, which reads as a bug at
            a glance. This test asserts the push is never REACHED, so the
            recording is what matters.

            Postconditions:
                - Appends ``a`` to ``push_calls`` and returns ``(True, None)``.
            """
            push_calls.append(a)
            return True, None

        monkeypatch.setattr(api, "_push_branch", _fake_push)
        update_calls = []
        monkeypatch.setattr(api, "update_job", lambda job_id, **kw: update_calls.append((job_id, kw)))

        with pytest.raises(RuntimeError, match="fast-forward failed: not a fast-forward"):
            _pr_publish()(_base_request())

        assert push_calls == []
        assert update_calls == []

    def test_push_failure_raises_and_skips_update(
        self, monkeypatch: pytest.MonkeyPatch, api: Any
    ) -> None:
        _stub_token(monkeypatch, api)
        monkeypatch.setattr(api, "_fast_forward", lambda *a, **kw: (True, None))
        monkeypatch.setattr(api, "_push_branch", lambda *a, **kw: (False, "remote rejected"))
        update_calls = []
        monkeypatch.setattr(api, "update_job", lambda job_id, **kw: update_calls.append((job_id, kw)))

        with pytest.raises(RuntimeError, match="git push failed: remote rejected"):
            _pr_publish()(_base_request())

        assert update_calls == []


class TestGithubPrPublishActivityStatusMapping:
    def _stub_git_ok(
        self, monkeypatch: pytest.MonkeyPatch, api: Any, clear_active_issue_calls: list | None = None
    ) -> None:
        monkeypatch.setattr(api, "_fast_forward", lambda *a, **kw: (True, None))
        monkeypatch.setattr(api, "_push_branch", lambda *a, **kw: (True, None))
        if clear_active_issue_calls is None:
            monkeypatch.setattr(api, "_clear_active_issue_if_matches", lambda *a, **kw: None)
        else:
            monkeypatch.setattr(
                api,
                "_clear_active_issue_if_matches",
                lambda *a, **kw: clear_active_issue_calls.append((a, kw)),
            )

    def test_all_tasks_passed_yields_completed(
        self, monkeypatch: pytest.MonkeyPatch, api: Any
    ) -> None:
        _stub_token(monkeypatch, api)
        clear_active_issue_calls: list = []
        self._stub_git_ok(monkeypatch, api, clear_active_issue_calls)
        job_row = {
            "job_id": "job-1",
            "task_graph_snapshot": [{"id": "t1", "status": "merged"}],
        }
        monkeypatch.setattr(api, "get_job", lambda job_id, cache_dir=None: dict(job_row))
        update_calls = []

        def _update_job(job_id: str, **kw: Any) -> None:
            update_calls.append((job_id, kw))
            job_row.update(kw)

        monkeypatch.setattr(api, "update_job", _update_job)

        request = _base_request()
        result = _pr_publish()(request)

        assert update_calls[0][1]["status"] == JobStatus.COMPLETED.value
        assert "failed task" not in update_calls[0][1]["status_text"]
        assert result["status"] == JobStatus.COMPLETED.value
        # The prep marker is cleared exactly once, keyed by this PR's own number
        # (see _clear_active_issue_if_matches -- the marker is generically keyed
        # by "the driving number", issue or PR).
        assert clear_active_issue_calls == [
            ((request["repo_path"], request["pr_number"]), {})
        ]

    def test_failed_task_yields_completed_with_failures(
        self, monkeypatch: pytest.MonkeyPatch, api: Any
    ) -> None:
        _stub_token(monkeypatch, api)
        self._stub_git_ok(monkeypatch, api)
        job_row = {
            "job_id": "job-1",
            "task_graph_snapshot": [
                {"id": "t1", "status": "merged"},
                {"id": "t2", "status": "failed"},
            ],
        }
        monkeypatch.setattr(api, "get_job", lambda job_id, cache_dir=None: dict(job_row))
        update_calls = []

        def _update_job(job_id: str, **kw: Any) -> None:
            update_calls.append((job_id, kw))
            job_row.update(kw)

        monkeypatch.setattr(api, "update_job", _update_job)

        result = _pr_publish()(_base_request())

        assert update_calls[0][1]["status"] == JobStatus.COMPLETED_WITH_FAILURES.value
        assert update_calls[0][1]["status_text"] == "Published fix to PR #7 with 1 failed task(s)"
        assert result["status"] == JobStatus.COMPLETED_WITH_FAILURES.value

    def test_pr_url_present_is_forwarded_to_update_job(
        self, monkeypatch: pytest.MonkeyPatch, api: Any
    ) -> None:
        _stub_token(monkeypatch, api)
        self._stub_git_ok(monkeypatch, api)
        monkeypatch.setattr(
            api, "get_job", lambda job_id, cache_dir=None: {"job_id": job_id, "task_graph_snapshot": []}
        )
        update_calls = []
        monkeypatch.setattr(api, "update_job", lambda job_id, **kw: update_calls.append((job_id, kw)))

        _pr_publish()(_base_request(pr_url="https://example/pull/7"))

        assert update_calls[0][1]["github_pr_url"] == "https://example/pull/7"

    def test_pr_url_absent_is_not_forwarded_to_update_job(
        self, monkeypatch: pytest.MonkeyPatch, api: Any
    ) -> None:
        _stub_token(monkeypatch, api)
        self._stub_git_ok(monkeypatch, api)
        monkeypatch.setattr(
            api, "get_job", lambda job_id, cache_dir=None: {"job_id": job_id, "task_graph_snapshot": []}
        )
        update_calls = []
        monkeypatch.setattr(api, "update_job", lambda job_id, **kw: update_calls.append((job_id, kw)))

        _pr_publish()(_base_request())

        assert "github_pr_url" not in update_calls[0][1]

    def test_none_job_after_update_falls_back_to_computed_status(
        self, monkeypatch: pytest.MonkeyPatch, api: Any
    ) -> None:
        _stub_token(monkeypatch, api)
        self._stub_git_ok(monkeypatch, api)
        # Token resolution (_require_activity_github_token) needs a real job on
        # its one lookup; every lookup the activity itself makes (failed-tasks
        # check, and the final return-value fetch) must come back None to
        # exercise the fallback dict.
        calls = {"n": 0}

        def _get_job(job_id: str, cache_dir=None):
            calls["n"] += 1
            return {"job_id": job_id} if calls["n"] == 1 else None

        monkeypatch.setattr(api, "get_job", _get_job)
        monkeypatch.setattr(api, "update_job", lambda job_id, **kw: None)

        result = _pr_publish()(_base_request())

        assert result == {
            "job_id": "job-1",
            "status": JobStatus.COMPLETED.value,
            "status_text": "Published fix to PR #7",
        }


class TestGithubBranchPrepActivityExpectedHeadSha:
    def test_expected_head_sha_forwarded_when_present(
        self, monkeypatch: pytest.MonkeyPatch, api: Any
    ) -> None:
        _stub_token(monkeypatch, api)
        captured: dict[str, Any] = {}

        def _fake_prepare(*_args: Any, **kwargs: Any):
            captured.update(kwargs)
            return True, None, []

        monkeypatch.setattr(api, "_prepare_issue_branch", _fake_prepare)

        result = _branch_prep()(
            {
                "job_id": "job-1",
                "repo_path": "/repo",
                "remote": "origin",
                "default_branch": "main",
                "integration_branch": "khala/pr-7",
                "expected_head_sha": "deadbeef",
            }
        )

        assert result == {"ok": True, "error": None, "notes": []}
        assert captured["expected_head_sha"] == "deadbeef"

    def test_expected_head_sha_defaults_to_none_when_absent(
        self, monkeypatch: pytest.MonkeyPatch, api: Any
    ) -> None:
        _stub_token(monkeypatch, api)
        captured: dict[str, Any] = {}

        def _fake_prepare(*_args: Any, **kwargs: Any):
            captured.update(kwargs)
            return True, None, []

        monkeypatch.setattr(api, "_prepare_issue_branch", _fake_prepare)

        _branch_prep()(
            {
                "job_id": "job-1",
                "repo_path": "/repo",
                "remote": "origin",
                "default_branch": "main",
                "integration_branch": "khala/issue-9",
            }
        )

        assert captured["expected_head_sha"] is None
