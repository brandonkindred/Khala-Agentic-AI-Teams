"""When PR review aborts for a missing engine provider, it must tell the PR.

The reviewer who invoked @khala-review watches the pull request, not the job
store — a silent abort leaves them waiting forever. The abort branch must mark
the job failed AND post a scrubbed one-line notice to the PR.
"""

from __future__ import annotations

from contextlib import contextmanager

import coding_team.api.main as main
from coding_team.api.main import ReviewPrRequest, _run_pr_review


def test_provider_abort_posts_pr_comment_and_fails_job(monkeypatch) -> None:
    monkeypatch.setattr(main, "get_engine_provider", lambda: None)

    job_updates: list = []
    review_updates: list = []
    comments: list = []
    monkeypatch.setattr(main, "update_job", lambda job_id, **kw: job_updates.append(kw))
    monkeypatch.setattr(main, "update_review", lambda job_id, **kw: review_updates.append(kw))

    @contextmanager
    def _fake_client(token: str):
        yield object()

    monkeypatch.setattr(main, "GitHubClient", _fake_client)
    monkeypatch.setattr(
        main,
        "_safe_comment",
        lambda client, owner, repo, number, body: comments.append((owner, repo, number, body)),
    )

    request = ReviewPrRequest(owner="acme", repo="widgets", pr_number=7)
    _run_pr_review("job-1", request, token="ghp_secret")

    # Job + review marked failed.
    assert any(u.get("status") == "failed" for u in job_updates)
    # The review row is marked failed AND carries the error (not just status_text),
    # consistent with _record_failure everywhere else.
    review_fail = next(u for u in review_updates if u.get("status") == "failed")
    assert "no engine provider" in (review_fail.get("error") or "")
    # Exactly one PR comment, on the right PR, naming the failure and no token.
    assert len(comments) == 1
    owner, repo, number, body = comments[0]
    assert (owner, repo, number) == ("acme", "widgets", 7)
    assert "no engine provider" in body
    assert "ghp_secret" not in body


def test_provider_abort_survives_github_outage(monkeypatch) -> None:
    """A GitHub failure while posting the abort notice must not turn into a raise —
    the job is already marked failed and that must stand."""
    monkeypatch.setattr(main, "get_engine_provider", lambda: None)
    monkeypatch.setattr(main, "update_job", lambda *a, **k: None)
    monkeypatch.setattr(main, "update_review", lambda *a, **k: None)

    @contextmanager
    def _boom_client(token: str):
        raise RuntimeError("github unreachable")
        yield  # pragma: no cover - unreachable

    monkeypatch.setattr(main, "GitHubClient", _boom_client)

    request = ReviewPrRequest(owner="acme", repo="widgets", pr_number=7)
    # Must not raise despite the GitHub outage.
    _run_pr_review("job-2", request, token="ghp_secret")
