"""Contract tests for ``SoftwareEngineeringAdapter`` and the registry.

Locks three things:
1. The adapter satisfies the ``TargetTeamAdapter`` Protocol shape.
2. URL construction matches the SE team's actual endpoint paths so the
   refactor doesn't silently drift from the contract.
3. The orchestrator handles ``status="cancelled"`` symmetrically across
   both phases — historically the analysis phase ignored cancellation
   and burned ~4h of polling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Reusable test doubles (mirror test_orchestrator_resume.py shapes)
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


class _FakeStore:
    def __init__(self, run: _FakeRun) -> None:
        self._run = run
        self.update_calls: list[dict[str, Any]] = []
        self.chat_messages: list[dict[str, Any]] = []

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
        pass


class _FakeResponse:
    def __init__(
        self, status_code: int = 200, json_data: dict | None = None, text: str = ""
    ) -> None:
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=MagicMock(), response=MagicMock(self)
            )


class _FakeHttpxClient:
    """Records every POST/GET; returns scripted responses keyed by URL substring."""

    def __init__(
        self,
        post_responses: dict[str, _FakeResponse] | None = None,
        get_responses: dict[str, list[_FakeResponse]] | None = None,
    ) -> None:
        self.post_responses = post_responses or {}
        self.get_responses = get_responses or {}
        self._get_indices: dict[str, int] = {}
        self.posts: list[dict[str, Any]] = []
        self.gets: list[dict[str, Any]] = []

    def __enter__(self) -> "_FakeHttpxClient":
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


@pytest.fixture
def stub_orchestrator_io(monkeypatch):
    from user_agent_founder import orchestrator

    monkeypatch.setattr(orchestrator.time, "sleep", lambda _s: None)
    monkeypatch.setattr(orchestrator, "ANALYSIS_POLL_INTERVAL", 0)
    monkeypatch.setattr(orchestrator, "EXECUTION_POLL_INTERVAL", 0)
    monkeypatch.setattr(orchestrator, "SPEC_HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(orchestrator, "_sync_job_status", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_heartbeat", lambda _rid: None)
    return orchestrator


# ---------------------------------------------------------------------------
# Protocol contract
# ---------------------------------------------------------------------------


def test_software_engineering_adapter_satisfies_protocol():
    from user_agent_founder.targets import SoftwareEngineeringAdapter, TargetTeamAdapter

    adapter = SoftwareEngineeringAdapter()
    # ``TargetTeamAdapter`` is ``runtime_checkable`` so this is a real
    # structural check, not just a type-hint formality.
    assert isinstance(adapter, TargetTeamAdapter)
    assert adapter.team_key == "software_engineering"
    assert adapter.display_name


def test_registry_returns_fresh_instance_per_call():
    from user_agent_founder.targets import get_adapter

    a = get_adapter("software_engineering")
    b = get_adapter("software_engineering")
    assert a is not b  # fresh instance, no cross-run state leakage
    assert type(a) is type(b)


def test_registry_rejects_unknown_team():
    from user_agent_founder.targets import get_adapter

    with pytest.raises(ValueError, match="does not support persona testing"):
        get_adapter("nonexistent_team")


# ---------------------------------------------------------------------------
# URL construction — locks the SE-side contract
# ---------------------------------------------------------------------------


def test_start_from_spec_hits_correct_endpoint():
    from user_agent_founder.targets import SoftwareEngineeringAdapter

    adapter = SoftwareEngineeringAdapter()
    fake = _FakeHttpxClient(
        post_responses={
            "/product-analysis/start-from-spec": _FakeResponse(200, {"job_id": "a-1"}),
        }
    )
    job_id = adapter.start_from_spec(fake, "proj", "# spec body")
    assert job_id == "a-1"
    assert fake.posts[0]["url"].endswith(
        "/api/software-engineering/product-analysis/start-from-spec"
    )
    assert fake.posts[0]["json"] == {"project_name": "proj", "spec_content": "# spec body"}


def test_start_from_spec_raises_on_http_error():
    from user_agent_founder.targets import SoftwareEngineeringAdapter, StartFailed

    adapter = SoftwareEngineeringAdapter()
    fake = _FakeHttpxClient(
        post_responses={
            "/product-analysis/start-from-spec": _FakeResponse(500, {}, text="boom"),
        }
    )
    with pytest.raises(StartFailed) as excinfo:
        adapter.start_from_spec(fake, "proj", "spec")
    assert excinfo.value.status_code == 500


def test_start_build_hits_correct_endpoint():
    from user_agent_founder.targets import SoftwareEngineeringAdapter

    adapter = SoftwareEngineeringAdapter()
    fake = _FakeHttpxClient(post_responses={"/run-team": _FakeResponse(200, {"job_id": "se-9"})})
    job_id = adapter.start_build(fake, "/repos/x")
    assert job_id == "se-9"
    assert fake.posts[0]["url"].endswith("/api/software-engineering/run-team")
    assert fake.posts[0]["json"] == {"repo_path": "/repos/x"}


def test_poll_methods_return_dict_and_signal_http_errors():
    from user_agent_founder.targets import SoftwareEngineeringAdapter

    adapter = SoftwareEngineeringAdapter()
    fake = _FakeHttpxClient(
        get_responses={
            "/product-analysis/status/job-1": [_FakeResponse(200, {"status": "running"})],
            "/run-team/job-2": [_FakeResponse(503, {})],
        }
    )
    assert adapter.poll_analysis(fake, "job-1") == {"status": "running"}
    payload = adapter.poll_build(fake, "job-2")
    assert payload.get("_poll_error") == 503


def test_submit_answers_post_to_correct_endpoints():
    from user_agent_founder.targets import SoftwareEngineeringAdapter

    adapter = SoftwareEngineeringAdapter()
    fake = _FakeHttpxClient(
        post_responses={
            "/product-analysis/job-1/answers": _FakeResponse(200),
            "/run-team/job-2/answers": _FakeResponse(200),
        }
    )
    answers = [{"question_id": "q1", "selected_option_id": "a"}]
    adapter.submit_analysis_answers(fake, "job-1", answers)
    adapter.submit_build_answers(fake, "job-2", answers)
    urls = [p["url"] for p in fake.posts]
    assert any(u.endswith("/api/software-engineering/product-analysis/job-1/answers") for u in urls)
    assert any(u.endswith("/api/software-engineering/run-team/job-2/answers") for u in urls)


# ---------------------------------------------------------------------------
# Symmetric cancellation handling — the bug this issue fixes
# ---------------------------------------------------------------------------


def _install_httpx(monkeypatch, orchestrator, fake_client) -> None:
    monkeypatch.setattr(orchestrator.httpx, "Client", lambda *a, **kw: fake_client)


def test_analysis_phase_aborts_on_cancelled_status(stub_orchestrator_io, monkeypatch):
    """Pre-fix: the analysis poll ignored ``cancelled`` and burned ~4h
    of MAX_POLL_ATTEMPTS before timing out. After this refactor, both
    phases share one helper that handles ``cancelled`` in one place."""
    from user_agent_founder.targets import SoftwareEngineeringAdapter

    orchestrator = stub_orchestrator_io
    run = _FakeRun(run_id="run-cancel-analysis")
    store = _FakeStore(run)
    agent = MagicMock()
    agent.generate_spec.return_value = "# spec"

    fake = _FakeHttpxClient(
        post_responses={
            "/product-analysis/start-from-spec": _FakeResponse(200, {"job_id": "a-1"}),
        },
        get_responses={
            # Single cancelled response — the loop must abort within one tick,
            # not consume MAX_POLL_ATTEMPTS.
            "/product-analysis/status/": [_FakeResponse(200, {"status": "cancelled"})],
        },
    )
    _install_httpx(monkeypatch, orchestrator, fake)

    orchestrator.run_workflow("run-cancel-analysis", store, agent, SoftwareEngineeringAdapter())

    assert run.status == "failed"
    assert run.error and "cancelled" in run.error.lower()
    # Critically: SE build was never started — cancel aborted Phase 2.
    assert not any("/run-team" in p["url"] for p in fake.posts)
    # And we polled at most a couple of times, not hundreds.
    analysis_polls = [g for g in fake.gets if "/product-analysis/status/" in g["url"]]
    assert len(analysis_polls) <= 3, (
        f"Expected analysis poll to abort on cancelled, got {len(analysis_polls)} polls"
    )


def test_build_phase_aborts_on_cancelled_status(stub_orchestrator_io, monkeypatch):
    """Regression: the build phase already handled cancelled — keep it that way."""
    from user_agent_founder.targets import SoftwareEngineeringAdapter

    orchestrator = stub_orchestrator_io
    run = _FakeRun(
        run_id="run-cancel-build",
        spec_content="# spec",
        analysis_job_id="a-1",
        repo_path="/repos/run-cancel-build",
    )
    store = _FakeStore(run)
    agent = MagicMock()

    fake = _FakeHttpxClient(
        post_responses={"/run-team": _FakeResponse(200, {"job_id": "se-1"})},
        get_responses={"/run-team/": [_FakeResponse(200, {"status": "cancelled"})]},
    )
    _install_httpx(monkeypatch, orchestrator, fake)

    orchestrator.run_workflow("run-cancel-build", store, agent, SoftwareEngineeringAdapter())

    assert run.status == "failed"
    assert run.error and "cancelled" in run.error.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
