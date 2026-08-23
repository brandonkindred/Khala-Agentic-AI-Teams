"""Tests for the shared BaseTeamLead / copy_development_result_fields."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shared.dev_models.models import Task, TaskStatus, TaskType
from software_engineering_team.shared.team_lead_base import (
    BaseTeamLead,
    TeamLeadSharedState,
    apply_team_failure,
    build_team_failure_result,
    copy_development_result_fields,
    warn_doc_agent_deprecated,
)
from software_engineering_team.shared.v2_models import Phase, SetupResult


def _make_lead(**overrides) -> BaseTeamLead:
    kwargs = dict(
        extensions=frozenset({".py"}),
        exclude_dirs=frozenset({".git"}),
        max_chars=1000,
    )
    kwargs.update(overrides)
    return BaseTeamLead(MagicMock(), **kwargs)


def test_init_requires_llm_client():
    with pytest.raises(AssertionError):
        BaseTeamLead(None, extensions=frozenset(), exclude_dirs=frozenset(), max_chars=100)


def test_init_stores_llm_and_starts_with_empty_cache_dict():
    llm = MagicMock()
    lead = BaseTeamLead(llm, extensions=frozenset({".py"}), exclude_dirs=frozenset(), max_chars=100)
    assert lead.llm is llm
    assert lead._repo_context_caches == {}
    assert lead._status_callback is None


def test_shared_state_defaults_and_stores_getter():
    llm = MagicMock(name="llm")
    getter = MagicMock(return_value=llm)
    state = TeamLeadSharedState(getter)
    assert state.llm_getter is getter
    assert state.shared_config == {}
    assert state._status_callback is None
    assert state._llm_for("worker-a") is llm
    getter.assert_called_once_with("worker-a")


def test_shared_state_shallow_copies_shared_config():
    original = {"mode": "swarm", "nested": {"k": 1}}
    state = TeamLeadSharedState(lambda _aid: None, shared_config=original)
    assert state.shared_config == original
    assert state.shared_config is not original
    original["mode"] = "mutated"
    original["nested"]["k"] = 99
    assert state.shared_config["mode"] == "swarm"
    # Shallow copy only: nested object is shared by design (YAGNI — no deep copy).
    assert state.shared_config["nested"]["k"] == 99
    state.shared_config["mode"] = "instance"
    assert original["mode"] == "mutated"


def test_shared_state_rejects_non_callable_getter():
    with pytest.raises(AssertionError):
        TeamLeadSharedState(None)  # type: ignore[arg-type]


def test_shared_state_rejects_non_mapping_config():
    with pytest.raises(AssertionError):
        TeamLeadSharedState(lambda _aid: None, shared_config=["bad"])  # type: ignore[arg-type]


def test_shared_state_llm_for_rejects_non_str_agent_id():
    state = TeamLeadSharedState(lambda _aid: None)
    with pytest.raises(AssertionError):
        state._llm_for(123)  # type: ignore[arg-type]


def test_base_team_lead_adapts_llm_client_to_getter():
    llm = MagicMock(name="lead-llm")
    lead = BaseTeamLead(
        llm,
        extensions=frozenset({".py"}),
        exclude_dirs=frozenset({".git"}),
        max_chars=100,
    )
    assert isinstance(lead, TeamLeadSharedState)
    assert lead.llm is llm
    assert lead.shared_config == {}
    assert lead._status_callback is None
    assert lead._llm_for("ignored") is llm
    assert lead._llm_for("") is llm


def test_repo_context_cache_for_is_lazy_and_reused(tmp_path: Path):
    lead = _make_lead()
    first = lead._repo_context_cache_for(tmp_path)
    second = lead._repo_context_cache_for(tmp_path)
    assert first is second  # same resolved repo -> reused, not rebuilt

    other = tmp_path / "other"
    other.mkdir()
    third = lead._repo_context_cache_for(other)
    assert third is not first  # distinct repo -> distinct cache


def test_repo_context_cache_for_uses_injected_constants(tmp_path: Path):
    extensions = frozenset({".ts", ".tsx"})
    exclude_dirs = frozenset({"dist"})
    lead = _make_lead(extensions=extensions, exclude_dirs=exclude_dirs, max_chars=42)
    cache = lead._repo_context_cache_for(tmp_path)
    assert cache._ext_set == extensions
    assert cache._excl_set == exclude_dirs
    assert cache._max_chars == 42


def test_repo_context_cache_for_rejects_non_directory(tmp_path: Path):
    lead = _make_lead()
    not_a_dir = tmp_path / "missing"
    with pytest.raises(AssertionError):
        lead._repo_context_cache_for(not_a_dir)


def test_copy_development_result_fields_copies_all_shared_fields():
    src = SimpleNamespace(
        success=True,
        current_phase="deliver",
        iterations_used=3,
        planning_result="planning",
        execution_result="execution",
        review_result="review",
        problem_solving_result="problem_solving",
        documentation_result="documentation",
        deliver_result="deliver",
        final_files={"a.py": "x = 1"},
        summary="done",
        failure_reason="",
        needs_followup=True,
        setup_result="src-setup",  # must NOT be copied
    )
    dst = SimpleNamespace(
        success=False,
        current_phase="setup",
        iterations_used=0,
        planning_result=None,
        execution_result=None,
        review_result=None,
        problem_solving_result=None,
        documentation_result=None,
        deliver_result=None,
        final_files={},
        summary="",
        failure_reason="",
        needs_followup=False,
        setup_result="dst-setup",  # must survive untouched
    )

    copy_development_result_fields(dst, src)

    assert dst.success is True
    assert dst.current_phase == "deliver"
    assert dst.iterations_used == 3
    assert dst.planning_result == "planning"
    assert dst.execution_result == "execution"
    assert dst.review_result == "review"
    assert dst.problem_solving_result == "problem_solving"
    assert dst.documentation_result == "documentation"
    assert dst.deliver_result == "deliver"
    assert dst.final_files == {"a.py": "x = 1"}
    assert dst.summary == "done"
    assert dst.needs_followup is True
    # setup_result is deliberately excluded from the copy.
    assert dst.setup_result == "dst-setup"


def _make_valid_pair():
    src = SimpleNamespace(
        success=True,
        current_phase="deliver",
        iterations_used=3,
        planning_result="planning",
        execution_result="execution",
        review_result="review",
        problem_solving_result="problem_solving",
        documentation_result="documentation",
        deliver_result="deliver",
        final_files={"a.py": "x = 1"},
        summary="done",
        failure_reason="",
        needs_followup=True,
        setup_result="src-setup",
    )
    dst = SimpleNamespace(
        success=False,
        current_phase="setup",
        iterations_used=0,
        planning_result=None,
        execution_result=None,
        review_result=None,
        problem_solving_result=None,
        documentation_result=None,
        deliver_result=None,
        final_files={},
        summary="",
        failure_reason="",
        needs_followup=False,
        setup_result="dst-setup",
    )
    return dst, src


def test_copy_development_result_fields_rejects_none_dst():
    _, src = _make_valid_pair()
    with pytest.raises(AssertionError):
        copy_development_result_fields(None, src)


def test_copy_development_result_fields_rejects_none_src():
    dst, _ = _make_valid_pair()
    with pytest.raises(AssertionError):
        copy_development_result_fields(dst, None)


def test_copy_development_result_fields_rejects_src_missing_field():
    dst, src = _make_valid_pair()
    del src.summary
    with pytest.raises(AssertionError):
        copy_development_result_fields(dst, src)


def test_copy_development_result_fields_rejects_dst_missing_field():
    dst, src = _make_valid_pair()
    del dst.summary
    with pytest.raises(AssertionError):
        copy_development_result_fields(dst, src)


class _FakeTeamResult:
    """Minimal constructor-shaped result for factory tests (devops-like)."""

    def __init__(self, *, success: bool = True, failure_reason: str = "", **extra):
        self.success = success
        self.failure_reason = failure_reason
        for key, value in extra.items():
            setattr(self, key, value)


def test_build_team_failure_result_sets_envelope():
    result = build_team_failure_result(_FakeTeamResult, "boom")
    assert result.success is False
    assert result.failure_reason == "boom"


def test_build_team_failure_result_forwards_partial_state():
    package = {"task_id": "t1", "status": "blocked"}
    result = build_team_failure_result(
        _FakeTeamResult,
        "Quality gates failed",
        completion_package=package,
        iterations=2,
    )
    assert result.success is False
    assert result.failure_reason == "Quality gates failed"
    assert result.completion_package == package
    assert result.iterations == 2


def test_build_team_failure_result_allows_empty_failure_reason():
    result = build_team_failure_result(_FakeTeamResult, "")
    assert result.success is False
    assert result.failure_reason == ""


def test_build_team_failure_result_rejects_success_override():
    with pytest.raises(AssertionError):
        build_team_failure_result(_FakeTeamResult, "x", success=True)


def test_build_team_failure_result_rejects_failure_reason_kwarg():
    with pytest.raises(TypeError):
        build_team_failure_result(_FakeTeamResult, "x", failure_reason="y")


def test_build_team_failure_result_rejects_non_callable_cls():
    with pytest.raises(AssertionError):
        build_team_failure_result(None, "x")  # type: ignore[arg-type]


def test_build_team_failure_result_rejects_non_str_failure_reason():
    with pytest.raises(AssertionError):
        build_team_failure_result(_FakeTeamResult, None)  # type: ignore[arg-type]


def test_apply_team_failure_mutates_in_place_and_returns_same_object():
    result = SimpleNamespace(success=True, failure_reason="", summary="keep-me", phase="setup")
    out = apply_team_failure(result, "Setup failed: disk full", phase="failed")
    assert out is result
    assert result.success is False
    assert result.failure_reason == "Setup failed: disk full"
    assert result.phase == "failed"
    assert result.summary == "keep-me"  # unrelated field preserved


def test_apply_team_failure_allows_empty_failure_reason():
    result = SimpleNamespace(success=True, failure_reason="old")
    apply_team_failure(result, "")
    assert result.success is False
    assert result.failure_reason == ""


def test_apply_team_failure_rejects_success_override():
    result = SimpleNamespace(success=True, failure_reason="")
    with pytest.raises(AssertionError):
        apply_team_failure(result, "x", success=True)


def test_apply_team_failure_rejects_failure_reason_kwarg():
    result = SimpleNamespace(success=True, failure_reason="")
    with pytest.raises(TypeError):
        apply_team_failure(result, "x", failure_reason="y")


def test_apply_team_failure_rejects_none_result():
    with pytest.raises(AssertionError):
        apply_team_failure(None, "x")


def test_apply_team_failure_rejects_non_str_failure_reason():
    result = SimpleNamespace(success=True, failure_reason="")
    with pytest.raises(AssertionError):
        apply_team_failure(result, None)  # type: ignore[arg-type]


def _make_task(task_id: str = "t1") -> Task:
    return Task(
        id=task_id,
        type=TaskType.BACKEND,
        assignee="backend-code-v2",
        status=TaskStatus.PENDING,
        title="T",
        description="D",
    )


def _fake_result_cls(*, task_id: str):
    return SimpleNamespace(
        task_id=task_id,
        success=False,
        current_phase=Phase.SETUP,
        iterations_used=0,
        setup_result=None,
        planning_result=None,
        execution_result=None,
        review_result=None,
        problem_solving_result=None,
        documentation_result=None,
        deliver_result=None,
        final_files={},
        summary="",
        failure_reason="",
        needs_followup=False,
    )


def test_run_setup_and_delegate_happy_path_copies_fields(tmp_path):
    lead = _make_lead()
    task = _make_task()
    inner = _fake_result_cls(task_id=task.id)
    inner.success = True
    inner.current_phase = Phase.DELIVER
    inner.iterations_used = 2
    inner.summary = "done"
    inner.needs_followup = True
    inner.final_files = {"a.py": "x"}

    class _DevAgent:
        def __init__(self, _llm):
            pass

        def run_workflow(self, **_kwargs):
            return inner

    job_calls: list[dict] = []

    result = lead._run_setup_and_delegate(
        repo_path=tmp_path,
        task=task,
        result_cls=_fake_result_cls,
        run_setup_fn=lambda **_k: SetupResult(linting_configured=True, testing_configured=True),
        development_agent_cls=_DevAgent,
        job_updater=lambda **kwargs: job_calls.append(kwargs),
        merge_to_development=False,
    )

    assert result.success is True
    assert result.current_phase == Phase.DELIVER
    assert result.iterations_used == 2
    assert result.summary == "done"
    assert result.needs_followup is True
    assert result.final_files == {"a.py": "x"}
    assert result.setup_result is not None
    assert result.setup_result.linting_configured is True
    assert any(c.get("progress") == 2 for c in job_calls)
    assert any(c.get("progress") == 3 for c in job_calls)
    assert any(c.get("progress") == 5 for c in job_calls)


def test_run_setup_and_delegate_forwards_doc_agent_unchanged(tmp_path):
    """doc_agent is still forwarded to the dev agent's run_workflow (not dropped early)."""
    lead = _make_lead()
    task = _make_task()
    received: dict = {}
    sentinel_doc_agent = object()

    class _DevAgent:
        def __init__(self, _llm):
            pass

        def run_workflow(self, **kwargs):
            received.update(kwargs)
            inner = _fake_result_cls(task_id=task.id)
            inner.success = True
            return inner

    lead._run_setup_and_delegate(
        repo_path=tmp_path,
        task=task,
        result_cls=_fake_result_cls,
        run_setup_fn=lambda **_k: SetupResult(linting_configured=True, testing_configured=True),
        development_agent_cls=_DevAgent,
        doc_agent=sentinel_doc_agent,
    )

    assert received["doc_agent"] is sentinel_doc_agent


def test_warn_doc_agent_deprecated_warns_on_non_none():
    with pytest.warns(DeprecationWarning, match="doc_agent"):
        warn_doc_agent_deprecated(MagicMock())


def test_warn_doc_agent_deprecated_no_warning_on_none(recwarn):
    warn_doc_agent_deprecated(None)
    assert not any(issubclass(w.category, DeprecationWarning) for w in recwarn.list)


def test_run_setup_and_delegate_allows_missing_repo_path(tmp_path):
    """Setup may create a nonexistent repo path (parity with run_setup_impl)."""
    missing = tmp_path / "new-repo"
    assert not missing.exists()
    lead = _make_lead()
    task = _make_task()
    seen: dict[str, Path] = {}

    def fake_setup(*, repo_path: Path, task_title: str = ""):
        seen["repo_path"] = repo_path
        Path(repo_path).mkdir(parents=True, exist_ok=True)
        return SetupResult(linting_configured=True, testing_configured=True)

    class _DevAgent:
        def __init__(self, _llm):
            pass

        def run_workflow(self, **_kwargs):
            inner = _fake_result_cls(task_id=task.id)
            inner.success = True
            return inner

    result = lead._run_setup_and_delegate(
        repo_path=missing,
        task=task,
        result_cls=_fake_result_cls,
        run_setup_fn=fake_setup,
        development_agent_cls=_DevAgent,
    )

    assert seen["repo_path"] == missing
    assert missing.is_dir()
    assert result.success is True


def test_run_setup_and_delegate_setup_exception_returns_early(tmp_path):
    lead = _make_lead()

    def boom(**_k):
        raise RuntimeError("disk full")

    result = lead._run_setup_and_delegate(
        repo_path=tmp_path,
        task=_make_task(),
        result_cls=_fake_result_cls,
        run_setup_fn=boom,
        development_agent_cls=type("NoAgent", (), {"__init__": lambda self, llm: None}),
    )

    assert result.success is False
    assert "Setup failed: disk full" in result.failure_reason


def test_run_setup_and_delegate_rejects_missing_linting(tmp_path):
    lead = _make_lead()
    result = lead._run_setup_and_delegate(
        repo_path=tmp_path,
        task=_make_task(),
        result_cls=_fake_result_cls,
        run_setup_fn=lambda **_k: SetupResult(linting_configured=False, testing_configured=True),
        development_agent_cls=type("NoAgent", (), {"__init__": lambda self, llm: None}),
    )
    assert "linting is not configured" in result.failure_reason.lower()


def test_run_setup_and_delegate_rejects_missing_testing(tmp_path):
    lead = _make_lead()
    result = lead._run_setup_and_delegate(
        repo_path=tmp_path,
        task=_make_task(),
        result_cls=_fake_result_cls,
        run_setup_fn=lambda **_k: SetupResult(linting_configured=True, testing_configured=False),
        development_agent_cls=type("NoAgent", (), {"__init__": lambda self, llm: None}),
    )
    assert "testing is not configured" in result.failure_reason.lower()


def test_run_setup_and_delegate_job_updater_failure_is_debug_logged(tmp_path, monkeypatch):
    import software_engineering_team.shared.team_lead_base as team_lead_base

    mock_debug = MagicMock()
    monkeypatch.setattr(team_lead_base.logger, "debug", mock_debug)

    lead = _make_lead()

    class _DevAgent:
        def __init__(self, _llm):
            pass

        def run_workflow(self, **_kwargs):
            return _fake_result_cls(task_id="t1")

    def bad_updater(**_kwargs):
        raise RuntimeError("job service down")

    result = lead._run_setup_and_delegate(
        repo_path=tmp_path,
        task=_make_task(),
        result_cls=_fake_result_cls,
        run_setup_fn=lambda **_k: SetupResult(linting_configured=True, testing_configured=True),
        development_agent_cls=_DevAgent,
        job_updater=bad_updater,
    )

    assert result is not None
    assert mock_debug.called
    logged = " ".join(str(arg) for call in mock_debug.call_args_list for arg in call[0])
    assert "job_updater failed" in logged


def test_run_setup_and_delegate_emits_canonical_status_text(tmp_path):
    lead = _make_lead()
    job_calls: list[dict] = []

    class _DevAgent:
        def __init__(self, _llm):
            pass

        def run_workflow(self, **_kwargs):
            return _fake_result_cls(task_id="t1")

    lead._run_setup_and_delegate(
        repo_path=tmp_path,
        task=_make_task(),
        result_cls=_fake_result_cls,
        run_setup_fn=lambda **_k: SetupResult(linting_configured=True, testing_configured=True),
        development_agent_cls=_DevAgent,
        job_updater=lambda **kwargs: job_calls.append(kwargs),
    )

    by_progress = {c["progress"]: c for c in job_calls if "progress" in c}
    assert by_progress[2]["status_text"] == "Setting up repository and development environment"
    assert by_progress[3]["status_text"] == "Repository setup complete"
    assert by_progress[5]["status_text"] == "Linting and testing verified; ready for development"


def test_report_status_noop_when_callback_unset():
    lead = _make_lead()
    assert lead._status_callback is None
    lead._report_status("phase1", detail="starting")  # must not raise


def test_report_status_forwards_hybrid_payload():
    lead = _make_lead()
    calls = []

    def _cb(**kwargs):
        calls.append(kwargs)

    lead._status_callback = _cb
    lead._report_status(
        "phase2",
        detail="change design",
        progress=0.4,
        status_text="DevOps phase 2",
    )
    assert calls == [
        {
            "phase": "phase2",
            "detail": "change design",
            "progress": 0.4,
            "status_text": "DevOps phase 2",
        }
    ]


def test_report_status_omits_none_progress():
    lead = _make_lead()
    calls = []
    lead._status_callback = lambda **kwargs: calls.append(kwargs)
    lead._report_status("phase3", detail="validation")
    assert calls == [{"phase": "phase3", "detail": "validation"}]
    assert "progress" not in calls[0]


def test_report_status_swallows_callback_errors():
    lead = _make_lead()

    def _boom(**_kwargs):
        raise RuntimeError("callback exploded")

    lead._status_callback = _boom
    lead._report_status("phase4", detail="review")  # must not raise


def test_report_status_logs_callback_errors(monkeypatch):
    import software_engineering_team.shared.team_lead_base as team_lead_base

    mock_warning = MagicMock()
    monkeypatch.setattr(team_lead_base.logger, "warning", mock_warning)
    lead = _make_lead()
    lead._status_callback = lambda **_k: (_ for _ in ()).throw(RuntimeError("callback exploded"))
    lead._report_status("phase4", detail="review")
    assert mock_warning.called
    logged = " ".join(str(arg) for call in mock_warning.call_args_list for arg in call[0])
    assert "team lead status callback failed" in logged
    assert "callback exploded" in logged


def test_report_status_rejects_empty_phase():
    lead = _make_lead()
    with pytest.raises(AssertionError):
        lead._report_status("")


def test_run_gated_phases_all_succeed_returns_none():
    lead = _make_lead()
    calls: list[str] = []

    def phase_a():
        calls.append("a")
        return None

    def phase_b():
        calls.append("b")
        return None

    def phase_c():
        calls.append("c")
        return None

    result = lead._run_gated_phases([phase_a, phase_b, phase_c])
    assert result is None
    assert calls == ["a", "b", "c"]


def test_run_gated_phases_early_exit_skips_later_phases():
    lead = _make_lead()
    calls: list[str] = []
    failure = object()

    def phase_ok():
        calls.append("ok")
        return None

    def phase_fail():
        calls.append("fail")
        return failure

    def phase_never():
        calls.append("never")
        return None

    result = lead._run_gated_phases([phase_ok, phase_fail, phase_never])
    assert result is failure
    assert calls == ["ok", "fail"]


def test_run_gated_phases_empty_sequence_returns_none():
    lead = _make_lead()
    assert lead._run_gated_phases([]) is None


def test_run_gated_phases_propagates_phase_exceptions():
    lead = _make_lead()

    def boom():
        raise RuntimeError("phase exploded")

    with pytest.raises(RuntimeError, match="phase exploded"):
        lead._run_gated_phases([boom])


@pytest.mark.parametrize("payload", [0, "", False], ids=["zero", "empty_str", "false"])
def test_run_gated_phases_treats_falsy_non_none_payload_as_failure(payload):
    lead = _make_lead()
    calls = []

    def phase_falsy():
        calls.append("falsy")
        return payload  # falsy but not None -> must be treated as failure

    def phase_never():
        calls.append("never")
        return None

    assert lead._run_gated_phases([phase_falsy, phase_never]) is payload
    assert calls == ["falsy"]


def test_run_phase_gates_all_succeed_returns_none():
    lead = _make_lead()
    calls: list[str] = []

    def gate_a():
        calls.append("a")
        return None

    def gate_b():
        calls.append("b")
        return None

    def gate_c():
        calls.append("c")
        return None

    result = lead._run_phase_gates([gate_a, gate_b, gate_c])
    assert result is None
    assert calls == ["a", "b", "c"]


def test_run_phase_gates_early_exit_skips_later_gates():
    lead = _make_lead()
    calls: list[str] = []
    failure = object()

    def gate_ok():
        calls.append("ok")
        return None

    def gate_fail():
        calls.append("fail")
        return failure

    def gate_never():
        calls.append("never")
        return None

    result = lead._run_phase_gates([gate_ok, gate_fail, gate_never])
    assert result is failure
    assert calls == ["ok", "fail"]


def test_run_phase_gates_empty_sequence_returns_none():
    lead = _make_lead()
    assert lead._run_phase_gates([]) is None


def test_run_phase_gates_propagates_gate_exceptions():
    lead = _make_lead()

    def boom():
        raise RuntimeError("gate exploded")

    with pytest.raises(RuntimeError, match="gate exploded"):
        lead._run_phase_gates([boom])


def test_bounded_retry_loop_success_on_first_attempt():
    lead = _make_lead()
    calls: list[int] = []

    def attempt(i: int):
        calls.append(i)
        return {"ok": True, "n": i}

    succeeded, result = lead._run_bounded_retry_loop(
        max_iterations=3,
        attempt=attempt,
        is_success=lambda r: r["ok"] is True,
    )
    assert succeeded is True
    assert result == {"ok": True, "n": 0}
    assert calls == [0]


def test_bounded_retry_loop_success_after_n_attempts():
    lead = _make_lead()
    calls: list[int] = []

    def attempt(i: int):
        calls.append(i)
        return {"ok": i >= 2, "n": i}

    succeeded, result = lead._run_bounded_retry_loop(
        max_iterations=5,
        attempt=attempt,
        is_success=lambda r: r["ok"] is True,
    )
    assert succeeded is True
    assert result == {"ok": True, "n": 2}
    assert calls == [0, 1, 2]


def test_bounded_retry_loop_exhausted_retries():
    lead = _make_lead()
    calls: list[int] = []

    def attempt(i: int):
        calls.append(i)
        return {"ok": False, "n": i}

    succeeded, result = lead._run_bounded_retry_loop(
        max_iterations=3,
        attempt=attempt,
        is_success=lambda r: r["ok"] is True,
    )
    assert succeeded is False
    assert result == {"ok": False, "n": 2}
    assert calls == [0, 1, 2]


def test_bounded_retry_loop_abort_on_none():
    lead = _make_lead()
    calls: list[int] = []

    def attempt(i: int):
        calls.append(i)
        if i == 1:
            return None
        return {"ok": False, "n": i}

    succeeded, result = lead._run_bounded_retry_loop(
        max_iterations=5,
        attempt=attempt,
        is_success=lambda r: r["ok"] is True,
    )
    assert succeeded is False
    assert result is None
    assert calls == [0, 1]


def test_bounded_retry_loop_rejects_non_positive_max_iterations():
    lead = _make_lead()
    with pytest.raises(AssertionError):
        lead._run_bounded_retry_loop(
            max_iterations=0,
            attempt=lambda _i: {"ok": True},
            is_success=lambda _r: True,
        )
