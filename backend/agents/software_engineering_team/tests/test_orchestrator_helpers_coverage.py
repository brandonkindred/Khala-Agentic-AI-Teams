"""Targeted unit tests for small orchestrator / api/main helper functions.

These tests cover pure helper functions that don't require a live LLM,
git workspace, or subprocess. The goal is to raise line coverage on the
already-instrumented helpers without exercising the integration-only
pipeline entry points (those are pragma'd separately).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _autouse_patched_job_store(patched_job_store):
    """Route the SE ``job_store._client`` factory through the in-memory fake."""
    return patched_job_store


# ---------------------------------------------------------------------------
# orchestrator helpers
# ---------------------------------------------------------------------------


def test_iso_now_returns_iso_string():
    import orchestrator

    out = orchestrator._iso_now()
    assert isinstance(out, str)
    # Must be parseable by datetime.fromisoformat (with optional Z)
    from datetime import datetime

    datetime.fromisoformat(out.replace("Z", "+00:00"))


def test_convert_to_structured_questions_assigns_unique_ids_and_options():
    import orchestrator

    qs = orchestrator._convert_to_structured_questions(
        ["What is the goal?", "What is the deadline?"], source="planning"
    )
    assert len(qs) == 2
    ids = {q["id"] for q in qs}
    assert len(ids) == 2  # unique
    for q in qs:
        assert q["question_text"] in ("What is the goal?", "What is the deadline?")
        assert q["options"]  # options copied from DEFAULT_CLARIFICATION_OPTIONS
        assert q["required"] is True
        assert q["source"] == "planning"


def test_convert_to_structured_questions_empty_list_returns_empty():
    import orchestrator

    assert orchestrator._convert_to_structured_questions([]) == []


def test_check_cancellation_raises_when_cancel_requested(monkeypatch):
    import orchestrator

    monkeypatch.setattr(orchestrator, "is_cancel_requested", lambda jid: True)
    with pytest.raises(orchestrator.CancellationError):
        orchestrator._check_cancellation("job-x")


def test_check_cancellation_silent_when_not_requested(monkeypatch):
    import orchestrator

    monkeypatch.setattr(orchestrator, "is_cancel_requested", lambda jid: False)
    # Should return None silently
    assert orchestrator._check_cancellation("job-x") is None


def test_wait_for_user_answers_returns_true_immediately(monkeypatch):
    """When the job is no longer waiting for answers, the helper returns True
    without entering the sleep loop."""
    import orchestrator

    monkeypatch.setattr(orchestrator, "is_waiting_for_answers", lambda _jid: False)
    assert orchestrator._wait_for_user_answers("job-x", timeout_seconds=10.0) is True


def test_wait_for_user_answers_returns_false_when_job_failed(monkeypatch):
    """If the job transitions to FAILED while waiting, the helper returns False."""
    import orchestrator

    monkeypatch.setattr(orchestrator, "is_waiting_for_answers", lambda _jid: True)
    monkeypatch.setattr(
        orchestrator,
        "get_job",
        lambda _jid: {"status": orchestrator.JOB_STATUS_FAILED},
    )
    # No-op sleep so the loop exits immediately on the failed-status check
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _s: None)
    assert orchestrator._wait_for_user_answers("job-x", timeout_seconds=10.0) is False


def test_get_task_stats_returns_zeros_with_empty_snapshot(monkeypatch):
    import orchestrator

    # Patch execution_tracker.snapshot to return no tasks
    monkeypatch.setattr(orchestrator.execution_tracker, "snapshot", lambda: {"tasks": []})
    stats = orchestrator._get_task_stats()
    assert stats == {
        "completed": 0,
        "in_progress": 0,
        "queued": 0,
        "total": 0,
        "percent": 0.0,
    }


def test_get_task_stats_computes_percent_with_completed_tasks(monkeypatch):
    import orchestrator

    monkeypatch.setattr(
        orchestrator.execution_tracker,
        "snapshot",
        lambda: {
            "tasks": [
                {"status": "done"},
                {"status": "done"},
                {"status": "in_progress"},
                {"status": "pending"},
            ]
        },
    )
    stats = orchestrator._get_task_stats()
    assert stats["completed"] == 2
    assert stats["in_progress"] == 1
    assert stats["queued"] == 1
    assert stats["total"] == 4
    assert stats["percent"] == 50.0


def test_parse_traceback_for_crash_extracts_top_frame():
    import orchestrator

    try:
        raise KeyError("missing")
    except KeyError as exc:
        path, line, func = orchestrator._parse_traceback_for_crash(exc)
    assert path is not None
    assert isinstance(line, int)
    assert func is not None


def test_parse_traceback_for_crash_handles_exception_without_tb():
    import orchestrator

    exc = KeyError("missing")
    # No __traceback__ → returns (None, None, None)
    path, line, func = orchestrator._parse_traceback_for_crash(exc)
    assert path is None
    assert line is None
    assert func is None


def test_log_agent_crash_banner_smoke():
    import orchestrator

    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        # Should not raise
        orchestrator._log_agent_crash_banner("task-x", "backend", exc)


def test_apply_repair_fixes_empty_list_returns_false(tmp_path: Path):
    import orchestrator

    assert orchestrator._apply_repair_fixes(tmp_path, []) is False


def test_apply_repair_fixes_skips_entry_without_file_path(tmp_path: Path):
    import orchestrator

    out = orchestrator._apply_repair_fixes(tmp_path, [{"line_start": 1, "line_end": 1}])
    assert out is False


def test_apply_repair_fixes_rejects_path_outside_agent_root(tmp_path: Path):
    import orchestrator

    other = tmp_path / "other"
    other.mkdir()
    target = other / "outside.py"
    target.write_text("x = 1\n", encoding="utf-8")
    agent_root = tmp_path / "agent_pkg"
    agent_root.mkdir()
    # Absolute path outside agent_root → rejected
    out = orchestrator._apply_repair_fixes(
        agent_root,
        [{"file_path": str(target), "line_start": 1, "line_end": 1, "replacement_content": "x"}],
    )
    assert out is False
    # Original file unchanged
    assert target.read_text(encoding="utf-8") == "x = 1\n"


def test_apply_repair_fixes_skips_missing_file(tmp_path: Path):
    import orchestrator

    out = orchestrator._apply_repair_fixes(
        tmp_path,
        [{"file_path": "missing.py", "line_start": 1, "line_end": 1, "replacement_content": "x"}],
    )
    assert out is False


def test_apply_repair_fixes_rejects_out_of_bounds_line_range(tmp_path: Path):
    import orchestrator

    target = tmp_path / "f.py"
    target.write_text("a\nb\n", encoding="utf-8")
    out = orchestrator._apply_repair_fixes(
        tmp_path,
        [
            {
                "file_path": "f.py",
                "line_start": 10,
                "line_end": 20,
                "replacement_content": "x",
            }
        ],
    )
    assert out is False
    assert target.read_text(encoding="utf-8") == "a\nb\n"  # unchanged


def test_apply_repair_fixes_applies_valid_replacement(tmp_path: Path):
    import orchestrator

    target = tmp_path / "f.py"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    out = orchestrator._apply_repair_fixes(
        tmp_path,
        [
            {
                "file_path": "f.py",
                "line_start": 2,
                "line_end": 2,
                "replacement_content": "BETA\n",
            }
        ],
    )
    assert out is True
    assert target.read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"


def test_issues_to_dicts_with_simple_objects():
    import orchestrator

    class _QABug:
        def model_dump(self):
            return {"description": "q1"}

    class _SecVuln:
        def model_dump(self):
            return {"description": "v1"}

    qa_list, sec_list = orchestrator._issues_to_dicts([_QABug()], [_SecVuln()])
    assert isinstance(qa_list, list) and qa_list and qa_list[0]["description"] == "q1"
    assert isinstance(sec_list, list) and sec_list and sec_list[0]["description"] == "v1"


def test_issues_to_dicts_with_none_inputs():
    import orchestrator

    qa_list, sec_list = orchestrator._issues_to_dicts(None, None)
    assert qa_list == []
    assert sec_list == []


def test_code_review_issues_to_dicts_handles_empty():
    import orchestrator

    assert orchestrator._code_review_issues_to_dicts([]) == []


def test_log_code_review_result_approved_path(caplog):
    import orchestrator

    review = MagicMock()
    review.approved = True
    review.summary = "looks good"
    review.issues = []
    # Should not raise
    orchestrator._log_code_review_result(review, "task-1")


def test_log_code_review_result_rejected_path(caplog):
    import orchestrator

    issue = MagicMock()
    issue.severity = "major"
    issue.category = "logic"
    issue.description = "buggy"
    issue.file_path = "a.py"
    issue.suggestion = "fix"
    review = MagicMock()
    review.approved = False
    review.summary = "issues found"
    review.issues = [issue]
    review.spec_compliance_notes = "ok"
    # Should not raise
    orchestrator._log_code_review_result(review, "task-1")


def test_log_code_review_result_rejected_zero_issues_warns(caplog):
    import orchestrator

    review = MagicMock()
    review.approved = False
    review.summary = ""
    review.issues = []
    review.spec_compliance_notes = ""
    # Should not raise; emits a warning about zero-issues-but-rejected
    orchestrator._log_code_review_result(review, "task-1")


def test_pop_runnable_task_picks_one_when_deps_met():
    import orchestrator

    class _T:
        def __init__(self, id_: str, deps: list[str]):
            self.id = id_
            self.dependencies = deps

    queue = ["a", "b"]
    all_tasks = {
        "a": _T("a", deps=["x"]),  # x not in completed
        "b": _T("b", deps=[]),  # runnable
    }
    completed: set[str] = set()
    out = orchestrator._pop_runnable_task(queue, all_tasks, completed)
    assert out == "b"
    assert "b" not in queue


def test_pop_runnable_task_returns_none_when_no_deps_satisfied():
    import orchestrator

    class _T:
        def __init__(self, id_: str, deps: list[str]):
            self.id = id_
            self.dependencies = deps

    queue = ["a"]
    all_tasks = {"a": _T("a", deps=["x"])}
    completed: set[str] = set()
    out = orchestrator._pop_runnable_task(queue, all_tasks, completed)
    assert out is None


def test_pop_runnable_task_skips_missing_task_objects():
    """If a task id is in the queue but not in all_tasks, it's skipped silently."""
    import orchestrator

    class _T:
        def __init__(self, id_: str, deps: list[str]):
            self.id = id_
            self.dependencies = deps

    queue = ["ghost", "real"]
    all_tasks = {"real": _T("real", deps=[])}
    out = orchestrator._pop_runnable_task(queue, all_tasks, set())
    assert out == "real"
    assert "ghost" in queue  # unchanged


def test_frontend_code_v2_worker_marks_failed_when_team_missing():
    """When the frontend_code_v2 team isn't registered, all queued tasks
    are marked failed with a clear reason and the worker returns
    without entering the integration loop."""
    import orchestrator

    queue = ["t1", "t2"]
    failed: dict = {}
    orchestrator._frontend_code_v2_worker(
        job_id="job-x",
        frontend_code_v2_queue=queue,
        all_tasks={},
        completed=set(),
        failed=failed,
        completed_code_task_ids=[],
        architecture=None,
        agents={},  # no frontend_code_v2 key
        repo_path=__import__("pathlib").Path("/tmp"),
    )
    assert failed == {
        "t1": "frontend_code_v2 team not registered",
        "t2": "frontend_code_v2 team not registered",
    }


def test_backend_code_v2_worker_marks_failed_when_team_missing():
    """Mirror coverage for the backend-code-v2 worker's no-team early-exit."""
    import orchestrator

    queue = ["t1"]
    failed: dict = {}
    orchestrator._backend_code_v2_worker(
        job_id="job-y",
        backend_code_v2_queue=queue,
        all_tasks={},
        completed=set(),
        failed=failed,
        completed_code_task_ids=[],
        architecture=None,
        agents={},  # no backend key
        repo_path=__import__("pathlib").Path("/tmp"),
    )
    assert failed == {"t1": "backend team not registered"}


def test_frontend_has_typescript_returns_true_when_ts_files(tmp_path: Path):
    import orchestrator

    (tmp_path / "a.ts").write_text("x", encoding="utf-8")
    assert orchestrator._frontend_has_typescript(tmp_path) is True


def test_frontend_has_typescript_returns_false_for_empty_dir(tmp_path: Path):
    import orchestrator

    assert orchestrator._frontend_has_typescript(tmp_path) is False


def test_initial_integration_outcome_not_run_when_inapplicable():
    import orchestrator

    out = orchestrator._initial_integration_outcome(
        integration_agent=MagicMock(),
        has_backend=True,
        has_frontend=False,
        completed_code_task_ids=["t1"],
    )
    assert out == "not_run"


def test_initial_integration_outcome_failed_when_agent_missing():
    import orchestrator

    out = orchestrator._initial_integration_outcome(
        integration_agent=None,
        has_backend=True,
        has_frontend=True,
        completed_code_task_ids=["t1"],
    )
    assert out == "failed"


def test_initial_integration_outcome_pending_when_runnable():
    import orchestrator

    out = orchestrator._initial_integration_outcome(
        integration_agent=MagicMock(),
        has_backend=True,
        has_frontend=True,
        completed_code_task_ids=["t1"],
    )
    assert out == "pending"


def test_log_task_breakdown_does_not_raise():
    import orchestrator

    class _T:
        def __init__(self, id_, assignee):
            self.id = id_
            self.assignee = assignee

    tasks = {
        "a": _T("a", "backend"),
        "b": _T("b", "frontend"),
        "c": _T("c", "custom_role"),  # falls through "other" branch
    }
    completed = {"a", "b", "c"}
    # Should not raise
    orchestrator._log_task_breakdown(
        completed=completed,
        all_tasks=tasks,
        total_tasks=3,
        failed_count=0,
        job_id="job-abc",
    )


def test_log_task_completion_banner_with_passing_task(monkeypatch):
    import orchestrator

    monkeypatch.setattr(
        orchestrator.execution_tracker,
        "snapshot",
        lambda: {"tasks": [{"status": "done"}]},
    )
    # Should not raise
    orchestrator._log_task_completion_banner(
        task_id="task-1",
        task_title="Implement feature",
        assignee="backend",
        elapsed_seconds=1.23,
        log_prefix="",
        description="Do the thing",
    )


def test_log_task_completion_banner_truncates_long_strings(monkeypatch):
    import orchestrator

    monkeypatch.setattr(
        orchestrator.execution_tracker,
        "snapshot",
        lambda: {"tasks": [{"status": "done"}]},
    )
    long_title = "X" * 200
    long_desc = "Y" * 200
    # Should not raise even when title/description exceed display width
    orchestrator._log_task_completion_banner(
        task_id="t" * 200,
        task_title=long_title,
        assignee="frontend",
        elapsed_seconds=42.0,
        log_prefix="retry",
        description=long_desc,
    )


# ---------------------------------------------------------------------------
# api/main helpers
# ---------------------------------------------------------------------------


def test_parse_task_states_none_when_input_empty():
    from software_engineering_team.api import main as api_main

    assert api_main._parse_task_states(None) is None
    assert api_main._parse_task_states({}) is None
    assert api_main._parse_task_states("not-a-dict") is None


def test_parse_task_states_skips_non_dict_entries():
    from software_engineering_team.api import main as api_main

    raw = {
        "t1": {"status": "pending", "assignee": "backend"},
        "t2": "not-a-dict",  # skipped
    }
    out = api_main._parse_task_states(raw)
    assert out is not None
    assert "t1" in out
    assert "t2" not in out


def test_parse_team_progress_handles_simple_entry():
    from software_engineering_team.api import main as api_main

    raw = {"backend": {"current_phase": "execution", "progress": 50}}
    out = api_main._parse_team_progress(raw)
    assert out is not None
    assert "backend" in out


def test_parse_team_progress_none_for_empty():
    from software_engineering_team.api import main as api_main

    assert api_main._parse_team_progress(None) is None
    assert api_main._parse_team_progress({}) is None
    assert api_main._parse_team_progress("foo") is None


def test_coerce_progress_handles_int_float_none():
    from software_engineering_team.api import main as api_main

    assert api_main._coerce_progress(None) is None
    assert api_main._coerce_progress(42) == 42
    assert api_main._coerce_progress(42.7) == 42
    assert api_main._coerce_progress("85") == 85


def test_coerce_progress_handles_non_numeric_string():
    from software_engineering_team.api import main as api_main

    # Non-numeric strings → None (try/except path)
    assert api_main._coerce_progress("not-a-number") is None


def test_get_workspace_base_dir_uses_se_workspace_dir(monkeypatch, tmp_path: Path):
    from software_engineering_team.api import main as api_main

    monkeypatch.setenv("SE_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("ENV_WORKSPACE_ROOT", raising=False)
    base = api_main._get_workspace_base_dir()
    assert base == tmp_path


def test_get_workspace_base_dir_falls_back_to_env_workspace_root(monkeypatch, tmp_path: Path):
    from software_engineering_team.api import main as api_main

    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.setenv("ENV_WORKSPACE_ROOT", str(tmp_path))
    base = api_main._get_workspace_base_dir()
    assert base == tmp_path


def test_get_workspace_base_dir_defaults_to_cwd_se_workspaces(monkeypatch):
    from software_engineering_team.api import main as api_main

    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("ENV_WORKSPACE_ROOT", raising=False)
    base = api_main._get_workspace_base_dir()
    assert base.name == "se_workspaces"


def test_create_project_workspace_creates_folder_with_initial_spec(monkeypatch, tmp_path: Path):
    from software_engineering_team.api import main as api_main

    monkeypatch.setenv("SE_WORKSPACE_DIR", str(tmp_path))
    ws = api_main.create_project_workspace("Project Name", b"# Spec\n")
    assert ws.exists()
    assert (ws / "initial_spec.md").read_text(encoding="utf-8") == "# Spec\n"
    # Sanitized: "project-name"
    assert "project-name" in ws.name


def test_create_project_workspace_rejects_empty_after_sanitization(tmp_path: Path):
    from software_engineering_team.api import main as api_main

    with pytest.raises(ValueError):
        api_main.create_project_workspace("@@@", b"x")


def test_create_project_workspace_rejects_empty_spec(monkeypatch, tmp_path: Path):
    from software_engineering_team.api import main as api_main

    monkeypatch.setenv("SE_WORKSPACE_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        api_main.create_project_workspace("good-name", b"   \n  ")


def test_preflight_sprint_scope_noop_when_none():
    from software_engineering_team.api import main as api_main

    # No sprint_id → returns silently
    assert api_main._preflight_sprint_scope(None) is None


def test_preflight_sprint_scope_404_when_sprint_missing(monkeypatch):
    from fastapi import HTTPException

    from software_engineering_team.api import main as api_main

    fake_store = MagicMock()
    fake_store.get_sprint_with_stories.return_value = None

    fake_module = MagicMock()
    fake_module.TERMINAL_STORY_STATUSES = {"done", "cancelled"}
    fake_module.ProductDeliveryStorageUnavailable = type("E", (Exception,), {})
    fake_module.get_store = lambda: fake_store

    with patch.dict("sys.modules", {"product_delivery": fake_module}):
        with pytest.raises(HTTPException) as exc:
            api_main._preflight_sprint_scope("sprint-x")
        assert exc.value.status_code == 404


def test_preflight_sprint_scope_400_when_no_stories(monkeypatch):
    from fastapi import HTTPException

    from software_engineering_team.api import main as api_main

    sprint_view = MagicMock()
    sprint_view.stories = []
    fake_store = MagicMock()
    fake_store.get_sprint_with_stories.return_value = sprint_view

    fake_module = MagicMock()
    fake_module.TERMINAL_STORY_STATUSES = {"done", "cancelled"}
    fake_module.ProductDeliveryStorageUnavailable = type("E", (Exception,), {})
    fake_module.get_store = lambda: fake_store

    with patch.dict("sys.modules", {"product_delivery": fake_module}):
        with pytest.raises(HTTPException) as exc:
            api_main._preflight_sprint_scope("sprint-x")
        assert exc.value.status_code == 400


def test_preflight_sprint_scope_400_when_all_terminal(monkeypatch):
    from fastapi import HTTPException

    from software_engineering_team.api import main as api_main

    s1 = MagicMock()
    s1.status = "done"
    s2 = MagicMock()
    s2.status = "Cancelled"  # case-insensitive
    sprint_view = MagicMock()
    sprint_view.stories = [s1, s2]
    fake_store = MagicMock()
    fake_store.get_sprint_with_stories.return_value = sprint_view

    fake_module = MagicMock()
    fake_module.TERMINAL_STORY_STATUSES = {"done", "cancelled"}
    fake_module.ProductDeliveryStorageUnavailable = type("E", (Exception,), {})
    fake_module.get_store = lambda: fake_store

    with patch.dict("sys.modules", {"product_delivery": fake_module}):
        with pytest.raises(HTTPException) as exc:
            api_main._preflight_sprint_scope("sprint-x")
        assert exc.value.status_code == 400


def test_preflight_sprint_scope_succeeds_when_executable_story_present():
    from software_engineering_team.api import main as api_main

    s_done = MagicMock()
    s_done.status = "done"
    s_active = MagicMock()
    s_active.status = "todo"  # not terminal
    sprint_view = MagicMock()
    sprint_view.stories = [s_done, s_active]
    fake_store = MagicMock()
    fake_store.get_sprint_with_stories.return_value = sprint_view

    fake_module = MagicMock()
    fake_module.TERMINAL_STORY_STATUSES = {"done", "cancelled"}
    fake_module.ProductDeliveryStorageUnavailable = type("E", (Exception,), {})
    fake_module.get_store = lambda: fake_store

    with patch.dict("sys.modules", {"product_delivery": fake_module}):
        # Should return None silently
        assert api_main._preflight_sprint_scope("sprint-x") is None


def test_preflight_sprint_scope_503_when_storage_unavailable():
    from fastapi import HTTPException

    from software_engineering_team.api import main as api_main

    class _Unavailable(Exception):
        pass

    fake_store = MagicMock()
    fake_store.get_sprint_with_stories.side_effect = _Unavailable("db down")

    fake_module = MagicMock()
    fake_module.TERMINAL_STORY_STATUSES = {"done", "cancelled"}
    fake_module.ProductDeliveryStorageUnavailable = _Unavailable
    fake_module.get_store = lambda: fake_store

    with patch.dict("sys.modules", {"product_delivery": fake_module}):
        with pytest.raises(HTTPException) as exc:
            api_main._preflight_sprint_scope("sprint-x")
        assert exc.value.status_code == 503


def test_is_orchestrator_alive_returns_false_for_unknown_job():
    from software_engineering_team.api import main as api_main

    # No thread registered → False
    assert api_main._is_orchestrator_alive("never-seen-job-id") is False


def test_get_spec_content_for_job_returns_empty_when_no_repo_path():
    from software_engineering_team.api import main as api_main

    assert api_main._get_spec_content_for_job({}) == ""


def test_get_spec_content_for_job_reads_via_spec_parser(tmp_path: Path):
    from software_engineering_team.api import main as api_main

    spec_text = "# Spec\nFeature X\n"
    with patch("spec_parser.get_latest_spec_content", return_value=spec_text):
        out = api_main._get_spec_content_for_job({"repo_path": str(tmp_path)})
    assert out == spec_text


def test_get_spec_content_for_job_returns_empty_on_file_not_found(tmp_path: Path):
    from software_engineering_team.api import main as api_main

    with patch("spec_parser.get_latest_spec_content", side_effect=FileNotFoundError("no spec")):
        out = api_main._get_spec_content_for_job({"repo_path": str(tmp_path)})
    assert out == ""


def test_get_spec_content_for_job_truncates_to_12000_chars(tmp_path: Path):
    from software_engineering_team.api import main as api_main

    huge = "X" * 20000
    with patch("spec_parser.get_latest_spec_content", return_value=huge):
        out = api_main._get_spec_content_for_job({"repo_path": str(tmp_path)})
    assert len(out) == 12000


def test_get_projects_root_uses_workspace_root_when_set(monkeypatch, tmp_path: Path):
    from software_engineering_team.api import main as api_main

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    root = api_main._get_projects_root()
    assert root.name == "projects"
    assert tmp_path in root.parents


def test_get_projects_root_defaults_to_tempdir_khala_projects(monkeypatch):
    from software_engineering_team.api import main as api_main

    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    root = api_main._get_projects_root()
    assert root.name == "khala_projects"


# --------------------------------------------------------------------------- _scale_progress


def test_scale_progress_maps_sub_agent_pct_onto_band():
    """Sub-agents report their own 0-100; the SE job updaters rescale onto the
    phase band so the bar is monotone across the run (no 100 → collapse handoffs)."""
    from software_engineering_team.orchestrator import (
        PROGRESS_BAND_CODING,
        PROGRESS_BAND_PLANNING,
        PROGRESS_BAND_PRODUCT_ANALYSIS,
        _scale_progress,
    )

    assert _scale_progress(0, PROGRESS_BAND_PRODUCT_ANALYSIS) == 0
    assert _scale_progress(100, PROGRESS_BAND_PRODUCT_ANALYSIS) == 15
    assert _scale_progress(100, PROGRESS_BAND_PLANNING) == 30
    assert _scale_progress(0, PROGRESS_BAND_CODING) == 30
    assert _scale_progress(100, PROGRESS_BAND_CODING) == 95
    # Bands tile the bar: each phase ends where the next begins, 95 < 100 leaves
    # room for the terminal write.
    assert _scale_progress(50, (0, 15)) == 7

    # Garbage degrades to None (caller drops the field); out-of-range clamps.
    assert _scale_progress("n/a", (0, 15)) is None
    assert _scale_progress(None, (0, 15)) is None
    assert _scale_progress(150, (0, 15)) == 15
    assert _scale_progress(-10, (0, 15)) == 0


def test_pra_and_planning_updaters_rescale_progress(monkeypatch):
    """The job updaters intercept a sub-agent 'progress' kwarg and rescale it; other
    kwargs pass through untouched and garbage progress is dropped, not written."""
    import software_engineering_team.orchestrator as se_orch

    written: list = []
    monkeypatch.setattr(se_orch, "update_job", lambda job_id, **kw: written.append(kw))

    updater = se_orch._make_planning_v3_job_updater("j1")
    updater(progress=100, status_text="done")
    assert written[-1]["progress"] == 30
    assert written[-1]["status_text"] == "done"

    updater(progress="garbage", status_text="odd")
    assert "progress" not in written[-1]
    assert written[-1]["status_text"] == "odd"
