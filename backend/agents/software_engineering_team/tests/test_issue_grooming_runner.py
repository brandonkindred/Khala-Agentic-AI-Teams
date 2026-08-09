"""Tests for IssueGroomingRunner (GitHub issue grooming Phase A -> Phase B orchestration)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from software_engineering_team.github_source.client import GitHubAPIError, Issue, SubIssue
from software_engineering_team.github_source.issue_grooming_runner import IssueGroomingRunner
from software_engineering_team.models import JobStatus


class _FakeGroomingClient:
    """Duck-typed GitHubClient fake exposing only what IssueGroomingRunner touches."""

    def __init__(self, issue: Issue, sub_issues: Optional[List[SubIssue]] = None) -> None:
        self._issue = issue
        self._sub_issues = sub_issues or []
        self._next_created_number = 900
        self.updated: List[Dict[str, Any]] = []
        self.created: List[Dict[str, Any]] = []
        self.linked: List[Dict[str, Any]] = []

    def get_issue(self, _owner: str, _repo: str, _number: int) -> Issue:
        return self._issue

    def update_issue(
        self, _owner: str, _repo: str, number: int, *, body=None, labels=None
    ) -> Issue:
        self.updated.append({"number": number, "body": body, "labels": labels})
        new_body = body if body is not None else self._issue.body
        new_labels = tuple(labels) if labels is not None else self._issue.labels
        self._issue = Issue(
            number=self._issue.number,
            title=self._issue.title,
            body=new_body,
            state=self._issue.state,
            html_url=self._issue.html_url,
            labels=new_labels,
            id=self._issue.id,
        )
        return self._issue

    def list_sub_issues(self, _owner: str, _repo: str, _number: int) -> List[SubIssue]:
        return list(self._sub_issues)

    def create_issue(self, _owner: str, _repo: str, *, title: str, body: str, labels=None) -> Issue:
        number = self._next_created_number
        self._next_created_number += 1
        self.created.append({"number": number, "title": title, "body": body})
        return Issue(
            number=number,
            title=title,
            body=body,
            state="open",
            html_url=f"https://example/issues/{number}",
            labels=tuple(labels or ()),
            id=200000 + number,
        )

    def add_sub_issue(self, _owner: str, _repo: str, issue_number: int, sub_issue_id: int) -> None:
        self.linked.append({"issue_number": issue_number, "sub_issue_id": sub_issue_id})


def _issue(
    number: int = 1,
    title: str = "Some issue",
    body: str = "A small change.",
    labels: tuple = (),
) -> Issue:
    return Issue(
        number=number,
        title=title,
        body=body,
        state="open",
        html_url=f"https://example/issues/{number}",
        labels=labels,
        id=100000 + number,
    )


def _job_store():
    """An in-memory update_job_fn/get_job_fn pair plus the call log, for assertions."""
    job: Dict[str, Any] = {}
    calls: List[Dict[str, Any]] = []

    def update_job_fn(**kwargs: Any) -> None:
        calls.append(dict(kwargs))
        job.update(kwargs)

    def get_job_fn(_job_id: str) -> Dict[str, Any]:
        return dict(job)

    return update_job_fn, get_job_fn, job, calls


class TestPhaseAOnly:
    def test_small_issue_scores_and_completes_without_split(self) -> None:
        issue = _issue(body="A small, simple change.")
        client = _FakeGroomingClient(issue)
        update_job_fn, get_job_fn, job, calls = _job_store()
        runner = IssueGroomingRunner(client, update_job_fn=update_job_fn, get_job_fn=get_job_fn)

        result = runner.run("job-1", "acme", "widget", 1)

        assert "score" in result
        assert "sub_issues" not in result
        assert client.created == []
        assert client.linked == []
        assert len(client.updated) == 1  # only Phase A's body/labels PATCH
        assert job["status"] == JobStatus.COMPLETED.value
        assert job["phase"] == "done"
        assert job["progress"] == 100

    def test_progress_is_monotonically_non_decreasing(self) -> None:
        client = _FakeGroomingClient(_issue())
        update_job_fn, get_job_fn, _job, calls = _job_store()
        runner = IssueGroomingRunner(client, update_job_fn=update_job_fn, get_job_fn=get_job_fn)

        runner.run("job-1", "acme", "widget", 1)

        progress_values = [c["progress"] for c in calls if "progress" in c]
        assert progress_values == sorted(progress_values)
        assert progress_values[-1] == 100

    def test_applies_complexity_label_and_body(self) -> None:
        client = _FakeGroomingClient(_issue(labels=("bug",)))
        update_job_fn, get_job_fn, _job, _calls = _job_store()
        runner = IssueGroomingRunner(client, update_job_fn=update_job_fn, get_job_fn=get_job_fn)

        runner.run("job-1", "acme", "widget", 1)

        first_update = client.updated[0]
        assert "## Complexity (Fibonacci)" in first_update["body"]
        assert "bug" in first_update["labels"]
        assert any(label.startswith("complexity: ") for label in first_update["labels"])

    def test_works_without_update_or_get_job_fn(self) -> None:
        client = _FakeGroomingClient(_issue())
        runner = IssueGroomingRunner(client)
        result = runner.run("job-1", "acme", "widget", 1)
        assert "score" in result


def _big_issue() -> Issue:
    body = "\n".join(
        [
            "This is a breaking change requiring a database migration and new authentication architecture.",
            "## Acceptance criteria",
            *[f"- [ ] step {i}" for i in range(5)],
        ]
    )
    return _issue(number=7, title="Big feature", body=body)


class TestPhaseBSplit:
    def test_splits_when_large_and_checklisted(self) -> None:
        issue = _big_issue()
        client = _FakeGroomingClient(issue)
        update_job_fn, get_job_fn, job, _calls = _job_store()
        runner = IssueGroomingRunner(client, update_job_fn=update_job_fn, get_job_fn=get_job_fn)

        result = runner.run("job-1", "acme", "widget", 7)

        assert len(client.created) == 5
        assert len(client.linked) == 5
        for entry in client.linked:
            assert entry["issue_number"] == 7
        assert result["sub_issues"]
        assert len(result["sub_issues"]) == 5
        # Parent body updated twice: once for Phase A's score, once for Phase B's sub-issues list.
        assert len(client.updated) == 2
        assert "## Sub-issues" in client.updated[-1]["body"]
        assert job["status"] == JobStatus.COMPLETED.value

    def test_skips_split_when_sub_issues_already_exist(self) -> None:
        issue = _big_issue()
        existing = [SubIssue(number=901, state="open", title="Already split")]
        client = _FakeGroomingClient(issue, sub_issues=existing)
        update_job_fn, get_job_fn, job, _calls = _job_store()
        runner = IssueGroomingRunner(client, update_job_fn=update_job_fn, get_job_fn=get_job_fn)

        result = runner.run("job-1", "acme", "widget", 7)

        assert client.created == []
        assert client.linked == []
        assert "sub_issues" not in result
        assert job["status"] == JobStatus.COMPLETED.value

    def test_small_issue_with_few_checklist_items_is_not_split(self) -> None:
        body = "## Acceptance criteria\n- [ ] one\n- [ ] two"
        issue = _issue(body=body)
        client = _FakeGroomingClient(issue)
        update_job_fn, get_job_fn, _job, _calls = _job_store()
        runner = IssueGroomingRunner(client, update_job_fn=update_job_fn, get_job_fn=get_job_fn)

        result = runner.run("job-1", "acme", "widget", 1)

        assert client.created == []
        assert "sub_issues" not in result


class TestIdempotentRerun:
    def test_rerun_replaces_phase_a_block_not_duplicates(self) -> None:
        client = _FakeGroomingClient(_issue())
        update_job_fn, get_job_fn, _job, _calls = _job_store()
        runner = IssueGroomingRunner(client, update_job_fn=update_job_fn, get_job_fn=get_job_fn)

        runner.run("job-1", "acme", "widget", 1)
        first_body = client.updated[-1]["body"]
        runner.run("job-1", "acme", "widget", 1)
        second_body = client.updated[-1]["body"]

        assert first_body.count("## Complexity (Fibonacci)") == 1
        assert second_body.count("## Complexity (Fibonacci)") == 1


class TestCancellation:
    def test_stops_before_phase_b_when_cancel_requested(self) -> None:
        issue = _big_issue()
        client = _FakeGroomingClient(issue)
        update_job_fn, _get_job_fn, job, _calls = _job_store()

        def get_job_fn(_job_id: str) -> Dict[str, Any]:
            return {"cancel_requested": True}

        runner = IssueGroomingRunner(client, update_job_fn=update_job_fn, get_job_fn=get_job_fn)
        result = runner.run("job-1", "acme", "widget", 7)

        assert client.created == []
        assert "sub_issues" not in result
        assert job["status"] == JobStatus.CANCELLED.value
        assert job["phase"] == "cancelled"


class TestPropagatesGitHubErrors:
    def test_get_issue_failure_propagates(self) -> None:
        class _FailingClient(_FakeGroomingClient):
            def get_issue(self, *_a: Any, **_kw: Any) -> Issue:
                raise GitHubAPIError(404, "missing")

        client = _FailingClient(_issue())
        update_job_fn, get_job_fn, _job, _calls = _job_store()
        runner = IssueGroomingRunner(client, update_job_fn=update_job_fn, get_job_fn=get_job_fn)

        with pytest.raises(GitHubAPIError):
            runner.run("job-1", "acme", "widget", 1)
