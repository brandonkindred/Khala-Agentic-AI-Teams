"""Unit tests for the fine-grained user_agent_founder Temporal activities.

The lifecycle was decomposed from one monolithic ``run_pipeline_activity`` (which
ran the whole orchestrator with blocking poll loops inside) into per-step
activities. Each ``@activity.defn`` is exercised directly with a MagicMock
store/agent/adapter, pinning: the resume/config snapshot ``begin_run`` returns,
the JSON-dict boundary, the ``StartFailed`` → non-retryable ``ApplicationError``
contract, the analysis→build ``repo_path`` handoff persisted on a completed
analysis poll, the answer-batch delegation + submit routing, the single-writer
COMPLETED/FAILED contracts, the cancel-not-clobbered guard, and the
missing-run → non-retryable failure guard shared by every activity.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest
from temporalio.exceptions import ApplicationError

import user_agent_founder.agent as agent_module
import user_agent_founder.targets as targets_module
from user_agent_founder import orchestrator, store
from user_agent_founder.shared import job_store
from user_agent_founder.temporal import activities as acts


def _run(**over):
    """Build a fake ``StoredRun`` (SimpleNamespace) with sensible defaults."""
    base = dict(
        run_id="r1",
        status="pending",
        se_job_id=None,
        analysis_job_id=None,
        spec_content=None,
        repo_path=None,
        target_team_key="software_engineering",
        persona_id=None,
        project_name=None,
        process_id=None,
        created_at="",
        updated_at="",
        error=None,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def _install(monkeypatch, *, run, adapter=None, job=None):
    """Patch the activities' lazy dependencies; return the wired mocks."""
    store_mock = MagicMock(name="store")
    store_mock.get_run.return_value = run
    monkeypatch.setattr(store, "get_founder_store", lambda: store_mock)

    persona_store = MagicMock(name="persona_store")
    persona_store.get_persona.return_value = None
    monkeypatch.setattr(store, "get_persona_store", lambda: persona_store)

    agent_mock = MagicMock(name="agent")
    monkeypatch.setattr(agent_module, "FounderAgent", lambda *a, **k: agent_mock)

    if adapter is None:
        adapter = MagicMock(name="adapter")
        adapter.display_name = "Software Engineering"
    monkeypatch.setattr(targets_module, "get_adapter", lambda *a, **k: adapter)

    monkeypatch.setattr(orchestrator, "_sync_job_status", MagicMock(name="_sync_job_status"))
    monkeypatch.setattr(orchestrator, "_heartbeat", MagicMock(name="_heartbeat"))
    monkeypatch.setattr(job_store, "get_job", lambda _jid: job)

    return types.SimpleNamespace(store=store_mock, agent=agent_mock, adapter=adapter)


# ---------------------------------------------------------------------------
# begin_run
# ---------------------------------------------------------------------------


def test_begin_run_snapshot_fresh(monkeypatch):
    _install(monkeypatch, run=_run())
    snap = acts.begin_run_activity("r1")

    assert snap["skip_spec"] is False
    assert snap["skip_analysis"] is False
    assert snap["analysis_job_id"] is None
    assert snap["build_job_id"] is None
    assert snap["adapter_display_name"] == "Software Engineering"
    # Env-derived tuning is read here (not in the deterministic workflow body).
    assert snap["analysis_poll_interval"] == orchestrator.ANALYSIS_POLL_INTERVAL
    assert snap["build_poll_interval"] == orchestrator.EXECUTION_POLL_INTERVAL
    assert snap["max_poll_attempts"] == orchestrator.MAX_POLL_ATTEMPTS
    assert snap["max_answer_retries"] == orchestrator.MAX_ANSWER_RETRIES
    orchestrator._sync_job_status.assert_called_once_with("r1", "running", phase="starting")


def test_begin_run_snapshot_resume(monkeypatch):
    run = _run(spec_content="SPEC", repo_path="/repo", analysis_job_id="aj", se_job_id="bj")
    m = _install(monkeypatch, run=run)
    snap = acts.begin_run_activity("r1")

    assert snap["skip_spec"] is True
    assert snap["skip_analysis"] is True
    assert snap["analysis_job_id"] == "aj"
    assert snap["build_job_id"] == "bj"
    assert snap["repo_path"] == "/repo"
    # Resume breadcrumbs recorded.
    kinds = [c.args[3] for c in m.store.add_chat_message.call_args_list]
    assert kinds.count("status_update") == 2


def test_begin_run_missing_row_is_non_retryable(monkeypatch):
    _install(monkeypatch, run=None)
    with pytest.raises(ApplicationError) as ei:
        acts.begin_run_activity("r1")
    assert ei.value.non_retryable is True


# ---------------------------------------------------------------------------
# generate_spec
# ---------------------------------------------------------------------------


def test_generate_spec_persists_and_returns_chars(monkeypatch):
    m = _install(monkeypatch, run=_run())
    monkeypatch.setattr(
        orchestrator, "_generate_spec_with_heartbeat", lambda _agent, _rid: "SPEC-CONTENT"
    )
    out = acts.generate_spec_activity("r1")

    assert out == {"chars": len("SPEC-CONTENT"), "skipped": False}
    m.store.update_run.assert_any_call("r1", spec_content="SPEC-CONTENT")


def test_generate_spec_skips_when_already_present(monkeypatch):
    _install(monkeypatch, run=_run(spec_content="EXISTING"))
    # Guard: the LLM path must not run when a spec already exists.
    monkeypatch.setattr(
        orchestrator,
        "_generate_spec_with_heartbeat",
        MagicMock(side_effect=AssertionError("must not regenerate")),
    )
    out = acts.generate_spec_activity("r1")
    assert out == {"chars": len("EXISTING"), "skipped": True}


def test_generate_spec_missing_row_is_non_retryable(monkeypatch):
    _install(monkeypatch, run=None)
    with pytest.raises(ApplicationError) as ei:
        acts.generate_spec_activity("r1")
    assert ei.value.non_retryable is True


def test_agent_uses_persona_prompts_when_present(monkeypatch):
    """A run with a persona builds the agent with that persona's prompts."""
    _install(monkeypatch, run=_run(persona_id="p1"))
    persona = types.SimpleNamespace(system_prompt="SYS", spec_generation_prompt="GEN")
    monkeypatch.setattr(
        store, "get_persona_store", lambda: types.SimpleNamespace(get_persona=lambda _pid: persona)
    )
    factory = MagicMock(return_value=MagicMock(name="agent"))
    monkeypatch.setattr(agent_module, "FounderAgent", factory)
    monkeypatch.setattr(orchestrator, "_generate_spec_with_heartbeat", lambda _a, _r: "SPEC")

    acts.generate_spec_activity("r1")

    factory.assert_called_once_with(system_prompt="SYS", spec_generation_prompt="GEN")


# ---------------------------------------------------------------------------
# enter_phase (fresh start + resume)
# ---------------------------------------------------------------------------


def test_enter_phase_analysis_records_job_id(monkeypatch):
    adapter = MagicMock(name="adapter")
    adapter.display_name = "Software Engineering"
    adapter.start_from_spec.return_value = "aj-1"
    m = _install(monkeypatch, run=_run(spec_content="SPEC"), adapter=adapter)

    out = acts.enter_phase_activity("r1", "analysis", None)

    assert out == {"job_id": "aj-1"}
    m.store.update_run.assert_any_call("r1", analysis_job_id="aj-1")
    m.store.update_run.assert_any_call("r1", status="polling_analysis", error=None)


def test_enter_phase_build_records_se_job_id(monkeypatch):
    adapter = MagicMock(name="adapter")
    adapter.display_name = "Software Engineering"
    adapter.start_build.return_value = "bj-1"
    m = _install(monkeypatch, run=_run(spec_content="SPEC", repo_path="/repo"), adapter=adapter)

    out = acts.enter_phase_activity("r1", "build", None)

    assert out == {"job_id": "bj-1"}
    m.store.update_run.assert_any_call("r1", se_job_id="bj-1")


def test_enter_phase_resume_transitions_polling_and_clears_error(monkeypatch):
    """A resumed phase (existing job id) must not re-submit, but must transition
    the run to polling_<phase> with the error cleared and a resume breadcrumb."""
    adapter = MagicMock(name="adapter")
    adapter.display_name = "Software Engineering"
    m = _install(
        monkeypatch, run=_run(spec_content="SPEC", analysis_job_id="aj-1"), adapter=adapter
    )

    out = acts.enter_phase_activity("r1", "analysis", "aj-1")

    assert out == {"job_id": "aj-1"}
    # No submit to the target on resume.
    adapter.start_from_spec.assert_not_called()
    m.store.update_run.assert_any_call("r1", status="polling_analysis", error=None)
    # Resume breadcrumb recorded.
    assert any(
        "Resuming" in (c.args[2] if len(c.args) > 2 else "")
        for c in m.store.add_chat_message.call_args_list
    )


def test_enter_phase_persisted_job_id_resumes_without_resubmit(monkeypatch):
    """Idempotency: a retry of a fresh submit (existing_job_id=None) whose prior
    attempt already persisted the job id must resume it, not submit a 2nd target job."""
    adapter = MagicMock(name="adapter")
    adapter.display_name = "Software Engineering"
    _install(monkeypatch, run=_run(spec_content="SPEC", analysis_job_id="aj-1"), adapter=adapter)

    out = acts.enter_phase_activity("r1", "analysis", None)

    assert out == {"job_id": "aj-1"}
    adapter.start_from_spec.assert_not_called()


def test_enter_phase_resume_skips_breadcrumb_when_already_polling(monkeypatch):
    """Retry idempotency: if the run is already polling_<phase> (a prior attempt
    ran), the resume path must not add a duplicate 'Resuming …' breadcrumb."""
    adapter = MagicMock(name="adapter")
    adapter.display_name = "Software Engineering"
    m = _install(
        monkeypatch,
        run=_run(spec_content="SPEC", analysis_job_id="aj-1", status="polling_analysis"),
        adapter=adapter,
    )

    acts.enter_phase_activity("r1", "analysis", "aj-1")

    assert not any(
        "Resuming" in (c.args[2] if len(c.args) > 2 else "")
        for c in m.store.add_chat_message.call_args_list
    )


def test_record_started_job_id_retries_transient_store_failure(monkeypatch):
    """A transient store failure on the just-started job-id persist is retried in
    the activity, so it does not fail the activity and force a duplicate submit."""
    monkeypatch.setattr(acts.time, "sleep", lambda _s: None)
    store = MagicMock(name="store")
    calls = {"n": 0}

    def _update(_run_id, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")

    store.update_run.side_effect = _update

    acts._record_started_job_id(store, "r1", "analysis", "aj-1")
    assert calls["n"] == 2  # failed once, then succeeded


def test_record_started_job_id_raises_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(acts.time, "sleep", lambda _s: None)
    store = MagicMock(name="store")
    store.update_run.side_effect = RuntimeError("store down")

    with pytest.raises(RuntimeError, match="store down"):
        acts._record_started_job_id(store, "r1", "build", "bj-1")
    assert store.update_run.call_count == acts._PERSIST_RETRIES


def test_enter_phase_start_failed_is_non_retryable(monkeypatch):
    from user_agent_founder.targets import StartFailed

    adapter = MagicMock(name="adapter")
    adapter.display_name = "Software Engineering"
    adapter.start_from_spec.side_effect = StartFailed(500, "boom")
    _install(monkeypatch, run=_run(spec_content="SPEC"), adapter=adapter)

    with pytest.raises(ApplicationError) as ei:
        acts.enter_phase_activity("r1", "analysis", None)
    assert ei.value.non_retryable is True
    assert "Failed to start analysis" in str(ei.value)


def test_enter_phase_unknown_phase_is_non_retryable(monkeypatch):
    _install(monkeypatch, run=_run(spec_content="SPEC"))
    with pytest.raises(ApplicationError) as ei:
        acts.enter_phase_activity("r1", "bogus", None)
    assert ei.value.non_retryable is True


# ---------------------------------------------------------------------------
# poll_phase
# ---------------------------------------------------------------------------


def test_poll_phase_analysis_completed_persists_repo_path(monkeypatch):
    adapter = MagicMock(name="adapter")
    adapter.poll_analysis.return_value = {"status": "completed", "repo_path": "/repo"}
    m = _install(monkeypatch, run=_run(spec_content="SPEC"), adapter=adapter)

    out = acts.poll_phase_activity("r1", "analysis", "aj-1")

    assert out["status"] == "completed"
    assert out["repo_path"] == "/repo"
    assert out["waiting"] is False
    m.store.update_run.assert_any_call("r1", repo_path="/repo")
    orchestrator._heartbeat.assert_called_once_with("r1")


def test_poll_phase_waiting_surfaces_questions(monkeypatch):
    adapter = MagicMock(name="adapter")
    adapter.poll_build.return_value = {
        "status": "running",
        "waiting_for_answers": True,
        "pending_questions": [{"id": "q1"}],
    }
    _install(monkeypatch, run=_run(spec_content="SPEC", repo_path="/repo"), adapter=adapter)

    out = acts.poll_phase_activity("r1", "build", "bj-1")

    assert out["waiting"] is True
    assert out["pending_questions"] == [{"id": "q1"}]


def test_poll_phase_poll_error_passed_through(monkeypatch):
    adapter = MagicMock(name="adapter")
    adapter.poll_analysis.return_value = {"_poll_error": "timeout", "status": ""}
    _install(monkeypatch, run=_run(spec_content="SPEC"), adapter=adapter)

    out = acts.poll_phase_activity("r1", "analysis", "aj-1")
    assert out["poll_error"] == "timeout"


def test_poll_phase_unknown_phase_is_non_retryable(monkeypatch):
    _install(monkeypatch, run=_run(spec_content="SPEC"))
    with pytest.raises(ApplicationError) as ei:
        acts.poll_phase_activity("r1", "bogus", "j")
    assert ei.value.non_retryable is True


# ---------------------------------------------------------------------------
# answer_questions
# ---------------------------------------------------------------------------


def test_answer_questions_delegates_and_routes_submit(monkeypatch):
    adapter = MagicMock(name="adapter")
    _install(monkeypatch, run=_run(spec_content="SPEC"), adapter=adapter)

    captured = {}

    def _fake_answer(agent, store_arg, run_id, job_id, questions, submit_fn):
        captured["args"] = (run_id, job_id, questions)
        # Exercise the submit routing wired by the activity.
        submit_fn([{"question_id": "q1", "selected_option_id": "a"}])
        return True

    monkeypatch.setattr(orchestrator, "_answer_pending_questions", _fake_answer)

    out = acts.answer_questions_activity("r1", "analysis", "aj-1", [{"id": "q1"}])

    assert out == {"ok": True}
    assert captured["args"] == ("r1", "aj-1", [{"id": "q1"}])
    adapter.submit_analysis_answers.assert_called_once()
    # Build phase routes to the build submitter instead.
    monkeypatch.setattr(
        orchestrator,
        "_answer_pending_questions",
        lambda *a, **k: a[5]([{"question_id": "q1", "selected_option_id": "a"}]) or True,
    )
    acts.answer_questions_activity("r1", "build", "bj-1", [{"id": "q1"}])
    adapter.submit_build_answers.assert_called_once()


def test_answer_questions_failure_returns_ok_false(monkeypatch):
    _install(monkeypatch, run=_run(spec_content="SPEC"))
    monkeypatch.setattr(orchestrator, "_answer_pending_questions", lambda *a, **k: False)
    out = acts.answer_questions_activity("r1", "analysis", "aj-1", [{"id": "q1"}])
    assert out == {"ok": False}


def test_answer_questions_unknown_phase_is_non_retryable(monkeypatch):
    _install(monkeypatch, run=_run(spec_content="SPEC"))
    with pytest.raises(ApplicationError) as ei:
        acts.answer_questions_activity("r1", "bogus", "j", [])
    assert ei.value.non_retryable is True


# ---------------------------------------------------------------------------
# finalize / mark_failed
# ---------------------------------------------------------------------------


def test_finalize_writes_completed(monkeypatch):
    m = _install(monkeypatch, run=_run(spec_content="SPEC", repo_path="/repo"))
    out = acts.finalize_run_activity("r1")

    assert out == {"run_id": "r1"}
    m.store.update_run.assert_any_call("r1", status="completed")
    orchestrator._sync_job_status.assert_any_call("r1", "completed", phase="completed")


def test_finalize_skips_breadcrumb_when_already_completed(monkeypatch):
    """Retry idempotency: a finalize retry (run already 'completed') must not add a
    duplicate 'Build completed' breadcrumb, though the status write stays idempotent."""
    m = _install(monkeypatch, run=_run(status="completed"))
    acts.finalize_run_activity("r1")

    assert not any(
        "Build completed" in (c.args[2] if len(c.args) > 2 else "")
        for c in m.store.add_chat_message.call_args_list
    )


def test_mark_failed_writes_failed_when_not_cancelled(monkeypatch):
    m = _install(monkeypatch, run=_run(), job={"status": job_store.JOB_STATUS_RUNNING})
    out = acts.mark_failed_activity("r1", "kaboom")

    assert out == {"marked": True}
    m.store.update_run.assert_any_call("r1", status="failed", error="kaboom")
    orchestrator._sync_job_status.assert_any_call("r1", "failed", error="kaboom")


def test_mark_failed_no_ops_when_cancelled(monkeypatch):
    m = _install(monkeypatch, run=_run(), job={"status": job_store.JOB_STATUS_CANCELLED})
    out = acts.mark_failed_activity("r1", "kaboom")

    assert out == {"marked": False}
    # The user cancel already wrote the terminal state — never clobbered.
    for c in m.store.update_run.call_args_list:
        assert c.kwargs.get("status") != "failed"


def test_mark_failed_no_ops_when_already_failed(monkeypatch):
    """Idempotency: a Temporal retry after the first attempt wrote FAILED must not
    re-write the status or add a duplicate failure breadcrumb."""
    m = _install(monkeypatch, run=_run(), job={"status": job_store.JOB_STATUS_FAILED})
    out = acts.mark_failed_activity("r1", "kaboom")

    assert out == {"marked": False}
    m.store.update_run.assert_not_called()
    m.store.add_chat_message.assert_not_called()
