"""Tests for the resume-short-circuit logic in ``run_workflow`` (#347).

PR #310 added ``POST /api/persona-testing/job/{id}/resume`` which simply
re-dispatches the workflow. Without these tests, ``run_workflow`` would
re-run every phase from the start regardless of which checkpoint columns
(``spec_content``, ``analysis_job_id``, ``repo_path``, ``se_job_id``) are
already populated on the run row — wasting an LLM spec call and a
multi-hour analysis poll on every resume.

Each test stubs the network (``httpx.Client``) and the founder store so
we can assert which orchestrator branches fire.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeRun:
    run_id: str
    status: str = "running"
    se_job_id: str | None = None
    analysis_job_id: str | None = None
    spec_content: str | None = None
    repo_path: str | None = None
    target_team_key: str = "software_engineering"
    created_at: str = "2026-04-25T00:00:00+00:00"
    updated_at: str = "2026-04-25T00:00:00+00:00"
    error: str | None = None


class FakeFounderStore:
    """In-memory stand-in for ``FounderRunStore``.

    Only implements the surface ``run_workflow`` actually calls.
    """

    def __init__(self, run: _FakeRun) -> None:
        self._run = run
        self.update_calls: list[dict[str, Any]] = []
        self.chat_messages: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []

    def get_run(self, run_id: str) -> _FakeRun | None:
        return self._run if self._run.run_id == run_id else None

    def update_run(self, run_id: str, **fields: Any) -> bool:
        self.update_calls.append({"run_id": run_id, **fields})
        for k, v in fields.items():
            setattr(self._run, k, v)
        return True

    def add_chat_message(
        self,
        run_id: str,
        role: str,
        content: str,
        message_type: str = "",
        *,
        metadata: Any = None,
    ) -> None:
        self.chat_messages.append(
            {"run_id": run_id, "role": role, "content": content, "type": message_type}
        )

    def add_decision(self, **fields: Any) -> None:
        self.decisions.append(fields)


class _FakeResponse:
    def __init__(
        self, status_code: int = 200, json_data: dict | None = None, text: str = ""
    ) -> None:
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self) -> dict:
        return self._json


class FakeHttpxClient:
    """Records every POST/GET; returns scripted responses keyed by URL substring."""

    def __init__(
        self,
        post_responses: dict[str, _FakeResponse] | None = None,
        get_responses: dict[str, list[_FakeResponse]] | None = None,
    ) -> None:
        self.post_responses = post_responses or {}
        # GET responses keyed by URL substring -> list of sequential responses
        self.get_responses = get_responses or {}
        self._get_indices: dict[str, int] = {}
        self.posts: list[dict[str, Any]] = []
        self.gets: list[dict[str, Any]] = []

    def __enter__(self) -> "FakeHttpxClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def post(self, url: str, *, json: dict | None = None, timeout: Any = None) -> _FakeResponse:
        self.posts.append({"url": url, "json": json})
        for needle, resp in self.post_responses.items():
            if needle in url:
                return resp
        return _FakeResponse(200, {"job_id": "default-job"})

    def get(self, url: str, *, timeout: Any = None) -> _FakeResponse:
        self.gets.append({"url": url})
        for needle, queue in self.get_responses.items():
            if needle in url:
                idx = self._get_indices.get(needle, 0)
                if idx >= len(queue):
                    return queue[-1]
                self._get_indices[needle] = idx + 1
                return queue[idx]
        return _FakeResponse(200, {"status": "completed"})


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_orchestrator_io(monkeypatch):
    """Neutralise sleeps + side-effecting helpers; isolate orchestrator under test."""
    from user_agent_founder import orchestrator

    monkeypatch.setattr(orchestrator.time, "sleep", lambda _s: None)
    monkeypatch.setattr(orchestrator, "ANALYSIS_POLL_INTERVAL", 0)
    monkeypatch.setattr(orchestrator, "EXECUTION_POLL_INTERVAL", 0)
    monkeypatch.setattr(orchestrator, "SPEC_HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(orchestrator, "_sync_job_status", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_heartbeat", lambda _rid: None)
    return orchestrator


def _install_httpx(monkeypatch, orchestrator, fake_client: FakeHttpxClient) -> None:
    monkeypatch.setattr(orchestrator.httpx, "Client", lambda *a, **kw: fake_client)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fresh_run_runs_all_phases(stub_orchestrator_io, monkeypatch):
    """Regression: empty checkpoints -> spec gen + analysis submit + build submit all fire."""
    orchestrator = stub_orchestrator_io
    run = _FakeRun(run_id="run-fresh")
    store = FakeFounderStore(run)
    agent = MagicMock()
    agent.generate_spec.return_value = "# Generated spec body"

    fake = FakeHttpxClient(
        post_responses={
            "/product-analysis/start-from-spec": _FakeResponse(200, {"job_id": "analysis-1"}),
            "/run-team": _FakeResponse(200, {"job_id": "se-1"}),
        },
        get_responses={
            "/product-analysis/status/": [
                _FakeResponse(200, {"status": "completed", "repo_path": "/repos/run-fresh"}),
            ],
            "/run-team/": [
                _FakeResponse(200, {"status": "completed"}),
            ],
        },
    )
    _install_httpx(monkeypatch, orchestrator, fake)

    orchestrator.run_workflow("run-fresh", store, agent)

    assert agent.generate_spec.call_count == 1
    assert any("/product-analysis/start-from-spec" in p["url"] for p in fake.posts)
    assert any(
        "/run-team" in p["url"] and "json" in p and p["json"] == {"repo_path": "/repos/run-fresh"}
        for p in fake.posts
    )
    assert run.status == "completed"


def test_resume_skips_phase1_when_spec_content_present(stub_orchestrator_io, monkeypatch):
    """spec_content set but nothing else -> no LLM call; analysis POST fires."""
    orchestrator = stub_orchestrator_io
    run = _FakeRun(run_id="run-1", spec_content="# pre-existing spec")
    store = FakeFounderStore(run)
    agent = MagicMock()

    fake = FakeHttpxClient(
        post_responses={
            "/product-analysis/start-from-spec": _FakeResponse(200, {"job_id": "analysis-1"}),
            "/run-team": _FakeResponse(200, {"job_id": "se-1"}),
        },
        get_responses={
            "/product-analysis/status/": [
                _FakeResponse(200, {"status": "completed", "repo_path": "/repos/run-1"}),
            ],
            "/run-team/": [
                _FakeResponse(200, {"status": "completed"}),
            ],
        },
    )
    _install_httpx(monkeypatch, orchestrator, fake)

    orchestrator.run_workflow("run-1", store, agent)

    agent.generate_spec.assert_not_called()
    assert any("/product-analysis/start-from-spec" in p["url"] for p in fake.posts)
    assert run.status == "completed"


def test_resume_skips_phase2_submit_when_analysis_job_id_present(stub_orchestrator_io, monkeypatch):
    """analysis_job_id set, repo_path empty -> no /start-from-spec POST; poll uses existing id."""
    orchestrator = stub_orchestrator_io
    run = _FakeRun(
        run_id="run-2",
        spec_content="# spec",
        analysis_job_id="prior-analysis-42",
    )
    store = FakeFounderStore(run)
    agent = MagicMock()

    fake = FakeHttpxClient(
        post_responses={
            "/run-team": _FakeResponse(200, {"job_id": "se-1"}),
        },
        get_responses={
            "/product-analysis/status/": [
                _FakeResponse(200, {"status": "completed", "repo_path": "/repos/run-2"}),
            ],
            "/run-team/": [
                _FakeResponse(200, {"status": "completed"}),
            ],
        },
    )
    _install_httpx(monkeypatch, orchestrator, fake)

    orchestrator.run_workflow("run-2", store, agent)

    agent.generate_spec.assert_not_called()
    assert not any("/product-analysis/start-from-spec" in p["url"] for p in fake.posts), (
        "Expected no analysis submit POST on resume with analysis_job_id set"
    )
    # First analysis GET hits the existing id (not a fresh one).
    analysis_gets = [g for g in fake.gets if "/product-analysis/status/" in g["url"]]
    assert analysis_gets, "Expected at least one analysis status GET"
    assert "prior-analysis-42" in analysis_gets[0]["url"]


def test_resume_skips_phase2_entirely_when_repo_path_present(stub_orchestrator_io, monkeypatch):
    """spec_content + repo_path set -> _run_product_analysis is not invoked; SE submit fires."""
    orchestrator = stub_orchestrator_io
    run = _FakeRun(
        run_id="run-3",
        spec_content="# spec",
        analysis_job_id="prior-analysis",
        repo_path="/repos/run-3",
    )
    store = FakeFounderStore(run)
    agent = MagicMock()

    fake = FakeHttpxClient(
        post_responses={
            "/run-team": _FakeResponse(200, {"job_id": "se-1"}),
        },
        get_responses={
            "/run-team/": [
                _FakeResponse(200, {"status": "completed"}),
            ],
        },
    )
    _install_httpx(monkeypatch, orchestrator, fake)

    orchestrator.run_workflow("run-3", store, agent)

    agent.generate_spec.assert_not_called()
    assert not any("/product-analysis" in g["url"] for g in fake.gets), (
        "Expected no analysis status polls when repo_path already set"
    )
    assert not any("/product-analysis" in p["url"] for p in fake.posts)
    se_posts = [p for p in fake.posts if "/run-team" in p["url"]]
    assert se_posts and se_posts[0]["json"] == {"repo_path": "/repos/run-3"}


def test_resume_skips_phase3_submit_when_se_job_id_present(stub_orchestrator_io, monkeypatch):
    """All checkpoints set incl. se_job_id -> no /run-team POST; poll uses existing id."""
    orchestrator = stub_orchestrator_io
    run = _FakeRun(
        run_id="run-4",
        spec_content="# spec",
        analysis_job_id="prior-analysis",
        repo_path="/repos/run-4",
        se_job_id="prior-se-99",
    )
    store = FakeFounderStore(run)
    agent = MagicMock()

    fake = FakeHttpxClient(
        get_responses={
            "/run-team/": [
                _FakeResponse(200, {"status": "completed"}),
            ],
        },
    )
    _install_httpx(monkeypatch, orchestrator, fake)

    orchestrator.run_workflow("run-4", store, agent)

    assert not any("/run-team" in p["url"] for p in fake.posts), (
        "Expected no SE-team submit POST on resume with se_job_id set"
    )
    se_gets = [g for g in fake.gets if "/run-team/" in g["url"]]
    assert se_gets, "Expected at least one SE-team status GET"
    assert "prior-se-99" in se_gets[0]["url"]
    assert run.status == "completed"


def test_resume_with_existing_analysis_job_succeeds_to_completion(
    stub_orchestrator_io, monkeypatch
):
    """Happy path: resume mid-Phase-2; analysis returns completed; Phase 3 then runs."""
    orchestrator = stub_orchestrator_io
    run = _FakeRun(
        run_id="run-5",
        spec_content="# spec",
        analysis_job_id="prior-analysis-77",
    )
    store = FakeFounderStore(run)
    agent = MagicMock()

    fake = FakeHttpxClient(
        post_responses={
            "/run-team": _FakeResponse(200, {"job_id": "se-new"}),
        },
        get_responses={
            "/product-analysis/status/": [
                _FakeResponse(200, {"status": "running"}),
                _FakeResponse(200, {"status": "completed", "repo_path": "/repos/run-5"}),
            ],
            "/run-team/": [
                _FakeResponse(200, {"status": "completed"}),
            ],
        },
    )
    _install_httpx(monkeypatch, orchestrator, fake)

    orchestrator.run_workflow("run-5", store, agent)

    agent.generate_spec.assert_not_called()
    se_posts = [p for p in fake.posts if "/run-team" in p["url"]]
    assert se_posts and se_posts[0]["json"] == {"repo_path": "/repos/run-5"}
    assert run.status == "completed"


def test_get_run_failure_is_caught_and_reported_as_failed(stub_orchestrator_io):
    """A transient store outage at the entry-time lookup must not escape the
    worker thread silently; the failure handler must mirror status to the
    founder store + job service so the UI doesn't see the run stuck."""
    orchestrator = stub_orchestrator_io
    store = MagicMock()
    store.get_run.side_effect = RuntimeError("postgres outage")
    agent = MagicMock()

    # Must not raise — exceptions in the worker thread are handled internally.
    orchestrator.run_workflow("run-boom", store, agent)

    # Failure path was hit: status flipped to "failed" + a chat message added.
    failed_calls = [
        c
        for c in store.update_run.call_args_list
        if c.kwargs.get("status") == "failed" and "postgres outage" in (c.kwargs.get("error") or "")
    ]
    assert failed_calls, (
        f"Expected update_run(..., status='failed', error=...) on get_run failure; "
        f"got {store.update_run.call_args_list}"
    )
    failure_msgs = [c for c in store.add_chat_message.call_args_list if "postgres outage" in str(c)]
    assert failure_msgs, "Expected a system chat message reporting the failure"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
