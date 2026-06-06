"""Tests for the SE Temporal activity wrappers.

Each activity is a thin wrapper around an orchestrator entry point with an
exception-handling outer try/except. The tests cover the happy path (the
wrapped function is called) and the exception path (failure is captured into
the job store via ``update_job``).
"""

from __future__ import annotations

from typing import Any, Dict

import pytest


@pytest.fixture(autouse=True)
def _autouse_patched_job_store(patched_job_store):
    return patched_job_store


def test_run_orchestrator_activity_success(monkeypatch, tmp_path) -> None:
    from software_engineering_team.temporal import activities

    called: Dict[str, Any] = {}

    def fake_run_orchestrator(
        job_id, repo_path, *, spec_content_override=None, resolved_questions_override=None, planning_only=False
    ):
        called.update(
            job_id=job_id,
            repo_path=repo_path,
            spec_override=spec_content_override,
            planning_only=planning_only,
        )

    monkeypatch.setattr(
        "software_engineering_team.orchestrator.run_orchestrator", fake_run_orchestrator
    )
    activities.run_orchestrator_activity("job1", str(tmp_path), spec_content_override="x", planning_only=True)
    assert called["job_id"] == "job1"
    assert called["planning_only"] is True


def test_run_orchestrator_activity_failure_captured(monkeypatch, tmp_path, patched_job_store) -> None:
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("job-x", repo_path=str(tmp_path))

    def boom(*a, **kw):
        raise RuntimeError("orchestrator crashed")

    monkeypatch.setattr("software_engineering_team.orchestrator.run_orchestrator", boom)
    activities.run_orchestrator_activity("job-x", str(tmp_path))
    job = js.get_job("job-x")
    assert job is not None
    assert job["status"] == js.JOB_STATUS_FAILED
    assert "orchestrator crashed" in (job.get("error") or "")


def test_retry_failed_activity_success(monkeypatch) -> None:
    from software_engineering_team.temporal import activities

    called: Dict[str, str] = {}

    def fake(job_id):
        called["id"] = job_id

    monkeypatch.setattr("software_engineering_team.orchestrator.run_failed_tasks", fake)
    activities.retry_failed_activity("j1")
    assert called["id"] == "j1"


def test_retry_failed_activity_failure(monkeypatch, tmp_path, patched_job_store) -> None:
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("j-fail", repo_path=str(tmp_path))

    def boom(_):
        raise RuntimeError("retry exploded")

    monkeypatch.setattr("software_engineering_team.orchestrator.run_failed_tasks", boom)
    activities.retry_failed_activity("j-fail")
    job = js.get_job("j-fail")
    assert job["status"] == js.JOB_STATUS_FAILED


def test_run_frontend_code_v2_activity_failure(monkeypatch, tmp_path, patched_job_store) -> None:
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("fv2-j", repo_path=str(tmp_path))

    def boom(*a, **kw):
        raise RuntimeError("v2 frontend failed")

    monkeypatch.setattr(activities, "_run_frontend_code_v2_impl", boom)
    activities.run_frontend_code_v2_activity("fv2-j", str(tmp_path), {"id": "t1"})
    job = js.get_job("fv2-j")
    assert job["status"] == js.JOB_STATUS_FAILED


def test_run_backend_code_v2_activity_failure(monkeypatch, tmp_path, patched_job_store) -> None:
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("bv2-j", repo_path=str(tmp_path))

    def boom(*a, **kw):
        raise RuntimeError("v2 backend failed")

    monkeypatch.setattr(activities, "_run_backend_code_v2_impl", boom)
    activities.run_backend_code_v2_activity("bv2-j", str(tmp_path), {"id": "t1"})
    job = js.get_job("bv2-j")
    assert job["status"] == js.JOB_STATUS_FAILED


def test_run_product_analysis_activity_failure(monkeypatch, tmp_path, patched_job_store) -> None:
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("pa-j", repo_path=str(tmp_path))

    def boom(*a, **kw):
        raise RuntimeError("PA failed")

    monkeypatch.setattr(activities, "_run_product_analysis_impl", boom)
    activities.run_product_analysis_activity("pa-j", str(tmp_path), "spec")
    job = js.get_job("pa-j")
    assert job["status"] == js.JOB_STATUS_FAILED


def test_run_frontend_code_v2_activity_happy(monkeypatch, tmp_path, patched_job_store) -> None:
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("fv2-ok", repo_path=str(tmp_path))
    called = {}

    def fake_impl(job_id, repo_path, task_dict, arch):
        called["job_id"] = job_id

    monkeypatch.setattr(activities, "_run_frontend_code_v2_impl", fake_impl)
    activities.run_frontend_code_v2_activity("fv2-ok", str(tmp_path), {"id": "t"}, "arch")
    assert called["job_id"] == "fv2-ok"


def test_run_backend_code_v2_activity_happy(monkeypatch, tmp_path, patched_job_store) -> None:
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("bv2-ok", repo_path=str(tmp_path))
    called = {}

    def fake_impl(job_id, repo_path, task_dict, arch):
        called["job_id"] = job_id

    monkeypatch.setattr(activities, "_run_backend_code_v2_impl", fake_impl)
    activities.run_backend_code_v2_activity("bv2-ok", str(tmp_path), {"id": "t"}, "arch")
    assert called["job_id"] == "bv2-ok"


def test_run_product_analysis_activity_happy(monkeypatch, tmp_path, patched_job_store) -> None:
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("pa-ok", repo_path=str(tmp_path))
    called = {}

    def fake_impl(job_id, repo_path, spec, initial_spec_path=None):
        called["job_id"] = job_id

    monkeypatch.setattr(activities, "_run_product_analysis_impl", fake_impl)
    activities.run_product_analysis_activity("pa-ok", str(tmp_path), "spec")
    assert called["job_id"] == "pa-ok"


def test_parse_spec_activity_exception_path(monkeypatch, tmp_path, patched_job_store) -> None:
    """No spec file in repo → spec parser raises FileNotFoundError, which the
    outer except in parse_spec_activity captures and re-raises after marking
    the job FAILED."""
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("ps-j", repo_path=str(tmp_path))
    with pytest.raises(Exception):
        activities.parse_spec_activity("ps-j", str(tmp_path))
    job = js.get_job("ps-j")
    assert job["status"] == js.JOB_STATUS_FAILED


def test_plan_project_activity_exception_path(monkeypatch, tmp_path, patched_job_store) -> None:
    """Cover the outer except in plan_project_activity.

    ``plan_project_activity`` calls ``parse_spec_with_llm(..., get_client("spec_intake"))``
    before it ever reaches ``_check_cancellation``, so patching the cancellation
    helper alone would let the activity make a real LLM call first and flake on
    transport errors. Instead, patch ``parse_spec_with_llm`` to raise — this
    deterministically drives the outer ``except`` branch without any network I/O.
    """
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("pp-j", repo_path=str(tmp_path))

    def boom(*a, **kw):
        raise RuntimeError("check failed")

    monkeypatch.setattr("spec_parser.parse_spec_with_llm", boom)
    with pytest.raises(RuntimeError):
        activities.plan_project_activity(
            "pp-j",
            str(tmp_path),
            {"spec_content": "spec", "validated_spec": "spec", "plan_dir": str(tmp_path)},
        )
    job = js.get_job("pp-j")
    assert job["status"] == js.JOB_STATUS_FAILED


def test_heartbeat_update_callback_emits_heartbeat(monkeypatch) -> None:
    """The coding-team update callback emits a Temporal heartbeat, then forwards the update."""
    from software_engineering_team.temporal import activities

    beats = {"n": 0}
    monkeypatch.setattr(
        activities.activity, "heartbeat", lambda *a, **k: beats.__setitem__("n", beats["n"] + 1)
    )
    captured: Dict[str, Any] = {}
    monkeypatch.setattr(
        activities, "update_job", lambda jid, **kw: captured.update({"jid": jid, **kw})
    )

    cb = activities._heartbeat_update_callback("job-x")
    cb(status_text="implementing")

    assert beats["n"] == 1
    assert captured["jid"] == "job-x"
    assert captured["status_text"] == "implementing"


def test_heartbeat_update_callback_swallows_heartbeat_error(monkeypatch) -> None:
    """Outside an activity context heartbeat() raises; the callback swallows it and still updates."""
    from software_engineering_team.temporal import activities

    def boom(*a, **k):
        raise RuntimeError("not in an activity context")

    monkeypatch.setattr(activities.activity, "heartbeat", boom)
    captured: Dict[str, Any] = {}
    monkeypatch.setattr(
        activities, "update_job", lambda jid, **kw: captured.update({"jid": jid, **kw})
    )

    cb = activities._heartbeat_update_callback("job-y")
    cb(phase="coding")  # must not raise despite the heartbeat failure

    assert captured["jid"] == "job-y"
    assert captured["phase"] == "coding"


def test_execute_coding_team_activity_exception_path(monkeypatch, tmp_path, patched_job_store) -> None:
    """Bogus adapter_result_dict triggers an exception inside the activity;
    the outer except marks the job FAILED and re-raises."""
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("ec-j", repo_path=str(tmp_path))
    with pytest.raises(Exception):
        activities.execute_coding_team_activity(
            "ec-j",
            str(tmp_path),
            {"adapter_result_dict": {}, "spec_content_for_planning": ""},
        )
    job = js.get_job("ec-j")
    assert job["status"] == js.JOB_STATUS_FAILED


def test_background_heartbeater_beats_until_stopped(monkeypatch) -> None:
    """The background heartbeater emits heartbeats repeatedly until the stop event is set."""
    import threading

    from software_engineering_team.temporal import activities

    beats = {"n": 0}
    stop = threading.Event()

    def hb(*a, **k):
        beats["n"] += 1
        if beats["n"] >= 3:
            stop.set()

    monkeypatch.setattr(activities.activity, "heartbeat", hb)
    t = activities._start_background_heartbeater(0.005, stop)
    t.join(timeout=2)

    assert not t.is_alive()
    assert beats["n"] >= 3


def test_background_heartbeater_swallows_errors(monkeypatch) -> None:
    """A raising heartbeat (e.g. outside an activity context) does not kill the beater loop."""
    import threading

    from software_engineering_team.temporal import activities

    calls = {"n": 0}
    stop = threading.Event()

    def hb(*a, **k):
        calls["n"] += 1
        if calls["n"] >= 2:
            stop.set()
        raise RuntimeError("no activity context")

    monkeypatch.setattr(activities.activity, "heartbeat", hb)
    t = activities._start_background_heartbeater(0.005, stop)
    t.join(timeout=2)

    assert not t.is_alive()  # errors swallowed; loop continued and exited cleanly on stop
    assert calls["n"] >= 2


def test_coding_heartbeat_interval_env(monkeypatch) -> None:
    """Interval: valid positive float honored; zero/negative/garbage/unset fall back to 30s."""
    from software_engineering_team.temporal import activities

    monkeypatch.setenv("CODING_TEAM_HEARTBEAT_INTERVAL_S", "12.5")
    assert activities._coding_heartbeat_interval_s() == 12.5
    monkeypatch.setenv("CODING_TEAM_HEARTBEAT_INTERVAL_S", "0")
    assert activities._coding_heartbeat_interval_s() == 30.0
    monkeypatch.setenv("CODING_TEAM_HEARTBEAT_INTERVAL_S", "-5")
    assert activities._coding_heartbeat_interval_s() == 30.0
    monkeypatch.setenv("CODING_TEAM_HEARTBEAT_INTERVAL_S", "garbage")
    assert activities._coding_heartbeat_interval_s() == 30.0
    monkeypatch.delenv("CODING_TEAM_HEARTBEAT_INTERVAL_S", raising=False)
    assert activities._coding_heartbeat_interval_s() == 30.0
