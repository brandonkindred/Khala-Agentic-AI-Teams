"""GitHub issue grooming Temporal module: Pattern-A exports, request/result
models, the real ``run_issue_grooming_activity`` body, and the still-stub
``IssueGroomingWorkflow``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest
from pydantic import ValidationError

from software_engineering_team.temporal.issue_grooming_workflow import (
    ACTIVITIES,
    WORKFLOWS,
    IssueGroomingRunRequest,
    IssueGroomingRunResult,
    IssueGroomingWorkflow,
    _grooming_heartbeat_interval_s,
    _grooming_update_callback,
    run_issue_grooming_activity,
)

_VALID_REQUEST = {"job_id": "j1", "owner": "acme", "repo": "widgets", "issue_number": 42}


@pytest.fixture
def patched_coding_job_store(monkeypatch: pytest.MonkeyPatch, fake_job_client):
    """Route the coding-team ``job_store._client`` factory through the in-memory fake.

    Mirrors ``conftest.py``'s ``patched_job_store`` fixture, but targets
    ``software_engineering_team.job_store`` (the coding-team job store this
    activity's ``job_id`` actually belongs to) rather than
    ``software_engineering_team.shared.job_store`` (a different job-store, used
    by the SE 4-phase pipeline).
    """
    from software_engineering_team import job_store as js

    monkeypatch.setattr(js, "_client", lambda cache_dir=js.DEFAULT_CACHE_DIR: fake_job_client)
    return fake_job_client


# ---------------------------------------------------------------------------
# Pattern-A export scaffold (unchanged by the real activity body)
# ---------------------------------------------------------------------------


def test_import_does_not_require_temporal_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing the module must not depend on ``TEMPORAL_ADDRESS`` being set.

    Preconditions: none.
    Postconditions: re-importing the already-loaded module raises nothing,
    proving no import-time worker boot or env read is load-bearing.
    """
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    import importlib

    import software_engineering_team.temporal.issue_grooming_workflow as mod

    importlib.reload(mod)


def test_exports_workflows_and_activities() -> None:
    assert WORKFLOWS == [IssueGroomingWorkflow]
    assert ACTIVITIES == [run_issue_grooming_activity]


def test_run_request_validates_required_fields() -> None:
    IssueGroomingRunRequest.model_validate(_VALID_REQUEST)
    with pytest.raises(ValidationError):
        IssueGroomingRunRequest.model_validate({"owner": "acme", "repo": "widgets"})


def test_run_result_defaults() -> None:
    result = IssueGroomingRunResult(status="pending")
    assert result.phase is None
    assert result.grooming is None


def test_workflow_stub_raises_not_implemented() -> None:
    workflow_obj = IssueGroomingWorkflow()
    with pytest.raises(NotImplementedError):
        asyncio.run(workflow_obj.run(_VALID_REQUEST))


def test_workflow_stub_validates_before_raising() -> None:
    workflow_obj = IssueGroomingWorkflow()
    with pytest.raises(ValidationError):
        asyncio.run(workflow_obj.run({"owner": "acme"}))


# ---------------------------------------------------------------------------
# run_issue_grooming_activity
# ---------------------------------------------------------------------------


def test_activity_validates_before_touching_job_or_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed request raises ValidationError before any job-store or GitHub I/O."""
    from software_engineering_team import job_store as js

    def _boom(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("job store must not be touched before request validation")

    monkeypatch.setattr(js, "get_job", _boom)
    monkeypatch.setattr(js, "update_job", _boom)

    with pytest.raises(ValidationError):
        run_issue_grooming_activity({"owner": "acme"})


def test_activity_missing_github_token_marks_job_failed(
    monkeypatch: pytest.MonkeyPatch, patched_coding_job_store
) -> None:
    from software_engineering_team import job_store as js

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    js.create_job("groom-1", repo_path="n/a")

    with pytest.raises(ValueError, match="GITHUB_TOKEN"):
        run_issue_grooming_activity({**_VALID_REQUEST, "job_id": "groom-1"})

    job = js.get_job("groom-1")
    assert job["status"] == "failed"
    assert "GITHUB_TOKEN" in job["error"]


def test_activity_success_path_returns_result(
    monkeypatch: pytest.MonkeyPatch, patched_coding_job_store
) -> None:
    import software_engineering_team.github_source.issue_grooming_runner as runner_module
    from software_engineering_team import job_store as js
    from software_engineering_team.models import JobStatus

    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    js.create_job("groom-2", repo_path="n/a")

    captured_init: Dict[str, Any] = {}

    class _StubRunner:
        def __init__(self, client, *, update_job_fn=None, get_job_fn=None) -> None:
            captured_init["update_job_fn"] = update_job_fn
            captured_init["get_job_fn"] = get_job_fn

        def run(self, job_id: str, owner: str, repo: str, issue_number: int) -> Dict[str, Any]:
            grooming = {"score": {"aggregate": 3}}
            captured_init["update_job_fn"](
                status=JobStatus.COMPLETED.value, phase="done", progress=100, grooming=grooming
            )
            return grooming

    monkeypatch.setattr(runner_module, "IssueGroomingRunner", _StubRunner)

    result = run_issue_grooming_activity({**_VALID_REQUEST, "job_id": "groom-2"})

    assert result == {
        "status": "completed",
        "phase": "done",
        "grooming": {"score": {"aggregate": 3}},
    }
    job = js.get_job("groom-2")
    assert job["status"] == JobStatus.COMPLETED.value
    assert captured_init["get_job_fn"]("groom-2") is not None


def test_activity_cancelled_path_reported_in_result(
    monkeypatch: pytest.MonkeyPatch, patched_coding_job_store
) -> None:
    import software_engineering_team.github_source.issue_grooming_runner as runner_module
    from software_engineering_team import job_store as js
    from software_engineering_team.models import JobStatus

    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    js.create_job("groom-3", repo_path="n/a")

    class _CancellingStubRunner:
        def __init__(self, client, *, update_job_fn=None, get_job_fn=None) -> None:
            self._update_job_fn = update_job_fn

        def run(self, job_id: str, owner: str, repo: str, issue_number: int) -> Dict[str, Any]:
            grooming = {"score": {"aggregate": 5}}
            self._update_job_fn(
                status=JobStatus.CANCELLED.value, phase="cancelled", grooming=grooming
            )
            return grooming

    monkeypatch.setattr(runner_module, "IssueGroomingRunner", _CancellingStubRunner)

    result = run_issue_grooming_activity({**_VALID_REQUEST, "job_id": "groom-3"})

    assert result["status"] == "cancelled"
    assert result["phase"] == "cancelled"


def test_activity_exception_path_marks_job_failed_and_reraises(
    monkeypatch: pytest.MonkeyPatch, patched_coding_job_store
) -> None:
    import software_engineering_team.github_source.issue_grooming_runner as runner_module
    from software_engineering_team import job_store as js

    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    js.create_job("groom-4", repo_path="n/a")

    class _RaisingStubRunner:
        def __init__(self, client, *, update_job_fn=None, get_job_fn=None) -> None:
            pass

        def run(self, job_id: str, owner: str, repo: str, issue_number: int) -> Dict[str, Any]:
            raise RuntimeError("boom")

    monkeypatch.setattr(runner_module, "IssueGroomingRunner", _RaisingStubRunner)

    with pytest.raises(RuntimeError, match="boom"):
        run_issue_grooming_activity({**_VALID_REQUEST, "job_id": "groom-4"})

    job = js.get_job("groom-4")
    assert job["status"] == "failed"
    assert job["error"] == "boom"


def test_grooming_heartbeat_interval_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid float honored; a parseable below-floor value clamps to 0.1; garbage/unset fall back to 30s."""
    monkeypatch.setenv("GITHUB_ISSUE_GROOMING_HEARTBEAT_INTERVAL_S", "12.5")
    assert _grooming_heartbeat_interval_s() == 12.5
    monkeypatch.setenv("GITHUB_ISSUE_GROOMING_HEARTBEAT_INTERVAL_S", "0")
    assert _grooming_heartbeat_interval_s() == 0.1
    monkeypatch.setenv("GITHUB_ISSUE_GROOMING_HEARTBEAT_INTERVAL_S", "-5")
    assert _grooming_heartbeat_interval_s() == 0.1
    monkeypatch.setenv("GITHUB_ISSUE_GROOMING_HEARTBEAT_INTERVAL_S", "garbage")
    assert _grooming_heartbeat_interval_s() == 30.0
    monkeypatch.delenv("GITHUB_ISSUE_GROOMING_HEARTBEAT_INTERVAL_S", raising=False)
    assert _grooming_heartbeat_interval_s() == 30.0


def test_grooming_update_callback_forwards_without_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The callback forwards kwargs to update_job and must NOT heartbeat.

    Liveness is owned solely by the background beater (single-liveness owner), so the
    update callback only persists progress.
    """
    from software_engineering_team import job_store as js
    from software_engineering_team.temporal import issue_grooming_workflow as mod

    captured: Dict[str, Any] = {}
    beats = {"n": 0}
    monkeypatch.setattr(js, "update_job", lambda jid, **kw: captured.update({"jid": jid, **kw}))
    monkeypatch.setattr(
        mod.activity, "heartbeat", lambda *a, **k: beats.__setitem__("n", beats["n"] + 1)
    )

    cb = _grooming_update_callback("job-x")
    cb(status_text="scoring")

    assert captured["jid"] == "job-x"
    assert captured["status_text"] == "scoring"
    assert beats["n"] == 0, "update callback must not emit a heartbeat (single-liveness owner)"
