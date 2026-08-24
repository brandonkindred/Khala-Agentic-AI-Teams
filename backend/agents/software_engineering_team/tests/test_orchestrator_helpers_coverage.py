"""Targeted unit tests for small orchestrator / api/main helper functions.

These tests cover pure helper functions that don't require a live LLM,
git workspace, or subprocess. The goal is to raise line coverage on the
already-instrumented helpers without exercising the integration-only
pipeline entry points (those are pragma'd separately).
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from shared.observability import bind_trace_id
from software_engineering_team import orchestrator


@pytest.fixture(autouse=True)
def _autouse_patched_job_store(patched_job_store):
    """Route the SE ``job_store._client`` factory through the in-memory fake."""
    return patched_job_store


# ---------------------------------------------------------------------------
# orchestrator helpers
# ---------------------------------------------------------------------------


def test_iso_now_returns_iso_string():
    """Iso now returns iso string."""
    out = orchestrator._iso_now()
    assert isinstance(out, str)
    # Must be parseable by datetime.fromisoformat (with optional Z)
    from datetime import datetime

    datetime.fromisoformat(out.replace("Z", "+00:00"))


def test_partition_tasks_by_completion_splits_and_preserves_order():
    """Partition tasks by completion splits and preserves order."""
    all_tasks = {"a": "ta", "b": "tb", "c": "tc", "d": "td"}
    completed_ids = {"a", "c"}
    remaining_ids = {"b", "d"}

    completed, remaining = orchestrator._partition_tasks_by_completion(
        all_tasks, completed_ids, remaining_ids
    )
    # Order follows all_tasks iteration order, matching the comprehensions it replaced.
    assert completed == ["ta", "tc"]
    assert remaining == ["tb", "td"]


def test_partition_tasks_by_completion_id_in_both_sets_appears_in_both():
    """Partition tasks by completion id in both sets appears in both."""
    all_tasks = {"a": "ta", "b": "tb"}
    # "a" is both completed and still listed as remaining: it must appear in both
    # lists, exactly as the two independent comprehensions produced.
    completed, remaining = orchestrator._partition_tasks_by_completion(all_tasks, {"a"}, {"a", "b"})
    assert completed == ["ta"]
    assert remaining == ["ta", "tb"]


def test_partition_tasks_by_completion_empty_sets_yield_empty_lists():
    """Partition tasks by completion empty sets yield empty lists."""
    completed, remaining = orchestrator._partition_tasks_by_completion({"a": "ta"}, set(), set())
    assert completed == []
    assert remaining == []


def test_partition_tasks_by_completion_rejects_non_set_ids():
    """The documented set precondition is enforced — passing a list (which would
    silently degrade membership to O(n)) raises rather than running."""
    with pytest.raises(AssertionError, match="completed_ids must be a set"):
        orchestrator._partition_tasks_by_completion({"a": "ta"}, ["a"], set())
    with pytest.raises(AssertionError, match="remaining_ids must be a set"):
        orchestrator._partition_tasks_by_completion({"a": "ta"}, set(), ["a"])


def test_convert_to_structured_questions_assigns_unique_ids_and_options():
    """Convert to structured questions assigns unique ids and options."""
    qs = orchestrator._convert_to_structured_questions(
        ["What is the goal?", "What is the deadline?"], source="planning"
    )
    assert len(qs) == 2
    ids = {q["id"] for q in qs}
    assert len(ids) == 2  # unique
    for q in qs:
        assert q["question_text"] in ("What is the goal?", "What is the deadline?")
        assert q["options"] == []  # empty-list fallback; UI shows free-text field only
        assert q["required"] is True
        assert q["source"] == "planning"


def test_convert_to_structured_questions_empty_list_returns_empty():
    """Convert to structured questions empty list returns empty."""
    assert orchestrator._convert_to_structured_questions([]) == []


def test_check_cancellation_raises_when_cancel_requested(monkeypatch):
    """Check cancellation raises when cancel requested."""
    monkeypatch.setattr(orchestrator, "is_cancel_requested", lambda jid: True)
    with pytest.raises(orchestrator.CancellationError):
        orchestrator._check_cancellation("job-x")


def test_check_cancellation_logs_bound_trace_id(monkeypatch, caplog):
    """The 'Cancellation detected' log carries the job's bound trace id via extra=,
    not plain string interpolation, so it can be correlated across phases."""
    monkeypatch.setattr(orchestrator, "is_cancel_requested", lambda jid: True)
    caplog.set_level(logging.INFO)
    with bind_trace_id("cancel-trace-id"), pytest.raises(orchestrator.CancellationError):
        orchestrator._check_cancellation("job-x")
    matching = [r for r in caplog.records if "Cancellation detected" in r.message]
    assert matching, "expected the cancellation log to be emitted"
    assert matching[-1].trace_id == "cancel-trace-id"


def test_check_cancellation_silent_when_not_requested(monkeypatch):
    """Check cancellation silent when not requested."""
    monkeypatch.setattr(orchestrator, "is_cancel_requested", lambda jid: False)
    # Should return None silently
    assert orchestrator._check_cancellation("job-x") is None


def test_wait_for_user_answers_returns_true_immediately(monkeypatch):
    """When the job is no longer waiting for answers, the helper returns True
    without entering the sleep loop."""

    monkeypatch.setattr(orchestrator, "is_waiting_for_answers", lambda _jid: False)
    assert orchestrator._wait_for_user_answers("job-x", timeout_seconds=10.0) is True


def test_wait_for_user_answers_returns_false_when_job_failed(monkeypatch):
    """If the job transitions to FAILED while waiting, the helper returns False."""

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
    # Patch execution_tracker.snapshot to return no tasks
    """Get task stats returns zeros with empty snapshot."""
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
    """Get task stats computes percent with completed tasks."""
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


# ---------------------------------------------------------------------------
# api/main helpers
# ---------------------------------------------------------------------------


def test_parse_task_states_none_when_input_empty():
    """Parse task states none when input empty."""
    from software_engineering_team.api import main as api_main

    assert api_main._parse_task_states(None) is None
    assert api_main._parse_task_states({}) is None
    assert api_main._parse_task_states("not-a-dict") is None


def test_parse_task_states_skips_non_dict_entries():
    """Parse task states skips non dict entries."""
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
    """Parse team progress handles simple entry."""
    from software_engineering_team.api import main as api_main

    raw = {"backend": {"current_phase": "execution", "progress": 50}}
    out = api_main._parse_team_progress(raw)
    assert out is not None
    assert "backend" in out


def test_parse_team_progress_none_for_empty():
    """Parse team progress none for empty."""
    from software_engineering_team.api import main as api_main

    assert api_main._parse_team_progress(None) is None
    assert api_main._parse_team_progress({}) is None
    assert api_main._parse_team_progress("foo") is None


def test_coerce_progress_handles_int_float_none():
    """Coerce progress handles int float none."""
    from software_engineering_team.api import main as api_main

    assert api_main._coerce_progress(None) is None
    assert api_main._coerce_progress(42) == 42
    assert api_main._coerce_progress(42.7) == 42
    assert api_main._coerce_progress("85") == 85


def test_coerce_progress_handles_non_numeric_string():
    """Coerce progress handles non numeric string."""
    from software_engineering_team.api import main as api_main

    # Non-numeric strings → None (try/except path)
    assert api_main._coerce_progress("not-a-number") is None


def test_get_workspace_base_dir_uses_se_workspace_dir(monkeypatch, tmp_path: Path):
    """Get workspace base dir uses se workspace dir."""
    from software_engineering_team.api import main as api_main

    monkeypatch.setenv("SE_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("ENV_WORKSPACE_ROOT", raising=False)
    base = api_main._get_workspace_base_dir()
    assert base == tmp_path


def test_get_workspace_base_dir_falls_back_to_env_workspace_root(monkeypatch, tmp_path: Path):
    """Get workspace base dir falls back to env workspace root."""
    from software_engineering_team.api import main as api_main

    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.setenv("ENV_WORKSPACE_ROOT", str(tmp_path))
    base = api_main._get_workspace_base_dir()
    assert base == tmp_path


def test_get_workspace_base_dir_defaults_to_cwd_se_workspaces(monkeypatch):
    """Get workspace base dir defaults to cwd se workspaces."""
    from software_engineering_team.api import main as api_main

    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("ENV_WORKSPACE_ROOT", raising=False)
    base = api_main._get_workspace_base_dir()
    assert base.name == "se_workspaces"


def test_create_project_workspace_creates_folder_with_initial_spec(monkeypatch, tmp_path: Path):
    """Create project workspace creates folder with initial spec."""
    from software_engineering_team.api import main as api_main

    monkeypatch.setenv("SE_WORKSPACE_DIR", str(tmp_path))
    ws = api_main.create_project_workspace("Project Name", b"# Spec\n")
    assert ws.exists()
    assert (ws / "initial_spec.md").read_text(encoding="utf-8") == "# Spec\n"
    # Sanitized: "project-name"
    assert "project-name" in ws.name


def test_create_project_workspace_rejects_empty_after_sanitization(tmp_path: Path):
    """Create project workspace rejects empty after sanitization."""
    from software_engineering_team.api import main as api_main

    with pytest.raises(ValueError):
        api_main.create_project_workspace("@@@", b"x")


def test_create_project_workspace_rejects_empty_spec(monkeypatch, tmp_path: Path):
    """Create project workspace rejects empty spec."""
    from software_engineering_team.api import main as api_main

    monkeypatch.setenv("SE_WORKSPACE_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        api_main.create_project_workspace("good-name", b"   \n  ")


def test_preflight_sprint_scope_noop_when_none():
    """Preflight sprint scope noop when none."""
    from software_engineering_team.api import main as api_main

    # No sprint_id → returns silently
    assert api_main._preflight_sprint_scope(None) is None


def test_preflight_sprint_scope_404_when_sprint_missing(monkeypatch):
    """Preflight sprint scope 404 when sprint missing."""
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
    """Preflight sprint scope 400 when no stories."""
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
    """Preflight sprint scope 400 when all terminal."""
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
    """Preflight sprint scope succeeds when executable story present."""
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
    """Preflight sprint scope 503 when storage unavailable."""
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
    """Is orchestrator alive returns false for unknown job."""
    from software_engineering_team.api import main as api_main

    # No thread registered → False
    assert api_main._is_orchestrator_alive("never-seen-job-id") is False


def test_get_spec_content_for_job_returns_empty_when_no_repo_path():
    """Get spec content for job returns empty when no repo path."""
    from software_engineering_team.api import main as api_main

    assert api_main._get_spec_content_for_job({}) == ""


def test_get_spec_content_for_job_reads_via_spec_parser(tmp_path: Path):
    """Get spec content for job reads via spec parser."""
    from software_engineering_team.api import main as api_main

    spec_text = "# Spec\nFeature X\n"
    with patch("software_engineering_team.spec_parser.get_latest_spec_content", return_value=spec_text):
        out = api_main._get_spec_content_for_job({"repo_path": str(tmp_path)})
    assert out == spec_text


def test_get_spec_content_for_job_returns_empty_on_file_not_found(tmp_path: Path):
    """Get spec content for job returns empty on file not found."""
    from software_engineering_team.api import main as api_main

    with patch("software_engineering_team.spec_parser.get_latest_spec_content", side_effect=FileNotFoundError("no spec")):
        out = api_main._get_spec_content_for_job({"repo_path": str(tmp_path)})
    assert out == ""


def test_get_spec_content_for_job_returns_full_content(tmp_path: Path):
    """Get spec content for job returns the full spec without truncation."""
    from software_engineering_team.api import main as api_main

    huge = "X" * 20000
    with patch("software_engineering_team.spec_parser.get_latest_spec_content", return_value=huge):
        out = api_main._get_spec_content_for_job({"repo_path": str(tmp_path)})
    assert out == huge
    assert len(out) == 20000


def test_get_projects_root_uses_workspace_root_when_set(monkeypatch, tmp_path: Path):
    """Get projects root uses workspace root when set."""
    from software_engineering_team.api import main as api_main

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    root = api_main._get_projects_root()
    assert root.name == "projects"
    assert tmp_path in root.parents


def test_get_projects_root_defaults_to_tempdir_khala_projects(monkeypatch):
    """Get projects root defaults to tempdir khala projects."""
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

    updater = se_orch._make_phase_job_updater(
        "j1",
        subprocess_key="planning_subprocess",
        completed_key="planning_completed_phases",
        phase_order=se_orch.PLANNING_PHASE_ORDER,
        progress_band=se_orch.PROGRESS_BAND_PLANNING,
    )
    updater(progress=100, status_text="done")
    assert written[-1]["progress"] == 30
    assert written[-1]["status_text"] == "done"

    updater(progress="garbage", status_text="odd")
    assert "progress" not in written[-1]
    assert written[-1]["status_text"] == "odd"


def test_make_phase_job_updater_edge_cases(monkeypatch):
    """Edge cases for the shared factory: empty/unmatched phase_order, falsy
    current_phase, garbage progress, and a None phase (no forced ``phase``
    kwarg on the update_job call)."""
    import software_engineering_team.orchestrator as se_orch

    written: list = []
    monkeypatch.setattr(se_orch, "update_job", lambda job_id, **kw: written.append(kw))

    # Empty phase_order: current_phase is never found, so x_subprocess is still
    # recorded but x_completed_phases is left unwritten (never the whole order).
    updater = se_orch._make_phase_job_updater(
        "j-empty",
        subprocess_key="x_subprocess",
        completed_key="x_completed_phases",
        phase_order=[],
        progress_band=(0, 15),
    )
    updater(current_phase="anything")
    assert written[-1]["x_subprocess"] == "anything"
    assert "x_completed_phases" not in written[-1]
    # phase=None (the default) forwards kwargs without forcing a "phase" key.
    assert "phase" not in written[-1]

    # current_phase not present in a non-empty phase_order: same fallback —
    # x_completed_phases stays unwritten rather than becoming the whole order.
    updater_with_order = se_orch._make_phase_job_updater(
        "j-order",
        subprocess_key="z_subprocess",
        completed_key="z_completed_phases",
        phase_order=["a", "b", "c"],
        progress_band=(0, 15),
    )
    updater_with_order(current_phase="unknown")
    assert written[-1]["z_subprocess"] == "unknown"
    assert "z_completed_phases" not in written[-1]

    # current_phase found mid-order: completed_phases is every entry before it.
    updater_with_order(current_phase="b")
    assert written[-1]["z_completed_phases"] == ["a"]

    # A falsy-but-non-None current_phase (empty string) is still recorded.
    updater_with_order(current_phase="")
    assert written[-1]["z_subprocess"] == ""

    # Garbage progress is dropped, never written.
    updater(current_phase="anything", progress="not-a-number")
    assert "progress" not in written[-1]

    # phase=<value> forces update_job's phase kwarg on every write.
    forced_updater = se_orch._make_phase_job_updater(
        "j-forced",
        subprocess_key="y_subprocess",
        completed_key="y_completed_phases",
        phase_order=["a", "b"],
        progress_band=(0, 15),
        phase="some_phase",
    )
    forced_updater(status_text="hi")
    assert written[-1]["phase"] == "some_phase"
    assert written[-1]["status_text"] == "hi"

    # update_job errors are swallowed (observability only, never raises).
    def _raise(job_id, **kw):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(se_orch, "update_job", _raise)
    forced_updater(status_text="should not raise")


# ---------------------------------------------------------------------------
# _make_planning_architecture_fn — the extracted Planning architecture callback
# ---------------------------------------------------------------------------


def _mock_arch_agent(*, overview="Arch overview", architecture_present=True, raises=None):
    """Build a duck-typed architecture agent whose ``run`` returns a scripted output."""
    agent = MagicMock()
    if raises is not None:
        agent.run.side_effect = raises
        return agent
    output = MagicMock()
    output.architecture = MagicMock(overview=overview) if architecture_present else None
    agent.run.return_value = output
    return agent


def test_planning_architecture_fn_spec_only_returns_overview():
    """Spec-only input returns the agent overview with the default acceptance criteria."""
    agent = _mock_arch_agent(overview="Arch overview")
    fn = orchestrator._make_planning_architecture_fn(lambda: agent)

    result = fn(spec_content="# Spec", prd_content=None, repo_path="/x", client_context=None)

    assert result == "Arch overview"
    arch_input = agent.run.call_args.args[0]
    assert arch_input.requirements.description == "# Spec"
    assert arch_input.requirements.acceptance_criteria == [
        "Deliver according to spec and planning artifacts."
    ]
    assert arch_input.features_and_functionality_doc is None


def test_planning_architecture_fn_prd_merges_into_description_and_features():
    """prd_content is appended to the requirements description and the features doc."""
    agent = _mock_arch_agent()
    fn = orchestrator._make_planning_architecture_fn(lambda: agent)

    fn(spec_content="Spec", prd_content="PRD body", repo_path="/x", client_context=None)

    arch_input = agent.run.call_args.args[0]
    assert arch_input.requirements.description == "Spec\n\nPRD body"
    assert arch_input.features_and_functionality_doc == "PRD body"


def test_planning_architecture_fn_success_criteria_override_acceptance():
    """client_context success_criteria replace the default acceptance criteria."""
    agent = _mock_arch_agent()
    fn = orchestrator._make_planning_architecture_fn(lambda: agent)

    fn(
        spec_content="Spec",
        prd_content=None,
        repo_path="/x",
        client_context={"success_criteria": ["c1", "c2"]},
    )

    arch_input = agent.run.call_args.args[0]
    assert arch_input.requirements.acceptance_criteria == ["c1", "c2"]


def test_planning_architecture_fn_no_tech_constraints_uses_default_preferences():
    """Without client_context tech_constraints, the module default preferences are used."""
    agent = _mock_arch_agent()
    fn = orchestrator._make_planning_architecture_fn(lambda: agent)

    fn(spec_content="Spec", prd_content=None, repo_path="/x", client_context=None)

    arch_input = agent.run.call_args.args[0]
    assert arch_input.technology_preferences == orchestrator._DEFAULT_TECHNOLOGY_PREFERENCES


def test_planning_architecture_fn_tech_constraints_override_default_preferences():
    """client_context tech_constraints replace the default technology_preferences."""
    agent = _mock_arch_agent()
    fn = orchestrator._make_planning_architecture_fn(lambda: agent)

    fn(
        spec_content="Spec",
        prd_content=None,
        repo_path="/x",
        client_context={"tech_constraints": ["Go", "Kubernetes"]},
    )

    arch_input = agent.run.call_args.args[0]
    assert arch_input.technology_preferences == ["Go", "Kubernetes"]


def test_planning_architecture_fn_problem_and_opportunity_build_features_and_goals():
    """problem_summary and opportunity_statement feed both the features doc and goals."""
    agent = _mock_arch_agent()
    fn = orchestrator._make_planning_architecture_fn(lambda: agent)

    fn(
        spec_content="Spec",
        prd_content=None,
        repo_path="/x",
        client_context={"problem_summary": "prob", "opportunity_statement": "opp"},
    )

    overview = agent.run.call_args.args[0].project_overview
    assert "## Problem summary\nprob" in overview["features_and_functionality_doc"]
    assert "## Opportunity\nopp" in overview["features_and_functionality_doc"]
    assert overview["goals"] == "prob\nopp"


def test_planning_architecture_fn_empty_spec_uses_fallback_description():
    """Empty spec and no prd fall back to the handoff-artifacts description."""
    agent = _mock_arch_agent()
    fn = orchestrator._make_planning_architecture_fn(lambda: agent)

    fn(spec_content="", prd_content=None, repo_path="/x", client_context=None)

    arch_input = agent.run.call_args.args[0]
    assert arch_input.requirements.description == "See Planning handoff artifacts."


def test_planning_architecture_fn_none_architecture_returns_none():
    """A response without an architecture yields None."""
    agent = _mock_arch_agent(architecture_present=False)
    fn = orchestrator._make_planning_architecture_fn(lambda: agent)

    assert fn(spec_content="Spec", prd_content=None, repo_path="/x", client_context=None) is None


def test_planning_architecture_fn_empty_overview_returns_empty_string():
    """An empty overview returns '' (distinct from the no-architecture None case)."""
    agent = _mock_arch_agent(overview="")
    fn = orchestrator._make_planning_architecture_fn(lambda: agent)

    assert fn(spec_content="Spec", prd_content=None, repo_path="/x", client_context=None) == ""


def test_planning_architecture_fn_agent_raises_returns_none():
    """The callback never propagates: an agent exception is swallowed to None."""
    agent = _mock_arch_agent(raises=RuntimeError("boom"))
    fn = orchestrator._make_planning_architecture_fn(lambda: agent)

    assert fn(spec_content="Spec", prd_content=None, repo_path="/x", client_context=None) is None


def test_planning_architecture_fn_provider_raises_returns_none():
    """A provider that cannot build the agent degrades to None, not an exception.

    This guards the lazy/defensive resolution: an agent-construction failure (e.g. no
    LLM provider configured) must not abort the Planning phase.
    """

    def _boom_provider():
        raise RuntimeError("no LLM configured")

    fn = orchestrator._make_planning_architecture_fn(_boom_provider)

    assert fn(spec_content="Spec", prd_content=None, repo_path="/x", client_context=None) is None


def test_planning_architecture_fn_resolves_provider_lazily_on_call():
    """The provider is not invoked at factory time, only when the callback runs."""
    calls = {"n": 0}
    agent = _mock_arch_agent(overview="Lazy overview")

    def _provider():
        calls["n"] += 1
        return agent

    fn = orchestrator._make_planning_architecture_fn(_provider)
    assert calls["n"] == 0, "provider must not be resolved until the callback is invoked"

    result = fn(spec_content="Spec", prd_content=None, repo_path="/x", client_context=None)

    assert result == "Lazy overview"
    assert calls["n"] == 1


def test_run_architecture_for_planning_module_level_success():
    """The module-level function itself (not just the factory closure) returns the overview."""
    agent = _mock_arch_agent(overview="Direct overview")

    result = orchestrator._run_architecture_for_planning(
        agent, spec_content="# Spec", prd_content=None, repo_path="/x", client_context=None
    )

    assert result == "Direct overview"


def test_run_architecture_for_planning_module_level_agent_raises_returns_none():
    """Calling the module-level function directly still swallows agent exceptions to None."""
    agent = _mock_arch_agent(raises=RuntimeError("boom"))

    result = orchestrator._run_architecture_for_planning(
        agent, spec_content="Spec", prd_content=None, repo_path="/x", client_context=None
    )

    assert result is None
