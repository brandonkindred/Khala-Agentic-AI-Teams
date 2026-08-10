"""The coding_team Temporal activity wires the orchestrator with a real job.

Guards the seam that a stale positional call broke: the activity must fail fast
and clearly when mis-wired (no provider / no plan), and otherwise call the
orchestrator against its real ``(job_id, repo_path, plan)`` signature.
"""

from __future__ import annotations

import pytest

from software_engineering_team.temporal.coding_team_workflow import run_pipeline_activity


def test_activity_raises_without_provider(monkeypatch) -> None:
    """The activity must fail fast when no CodeEngineProvider is available.

    Preconditions: ``get_engine_provider`` returns ``None``.
    Postconditions: ``run_pipeline_activity`` raises ``RuntimeError`` matching
    "no CodeEngineProvider" without attempting any orchestration work.
    """
    import software_engineering_team.engine_provider as ep

    monkeypatch.setattr(ep, "get_engine_provider", lambda: None)
    with pytest.raises(RuntimeError, match="no CodeEngineProvider"):
        run_pipeline_activity({"repo_path": "/tmp/x", "plan_input": {"objective": "o"}})


def test_activity_raises_without_plan(monkeypatch) -> None:
    """The activity must reject a missing ``plan_input`` before orchestration.

    Preconditions: a provider is available but ``plan_input`` is ``None``.
    Postconditions: ``run_pipeline_activity`` raises ``ValueError`` matching
    "requires a plan_input".
    """
    import software_engineering_team.engine_provider as ep

    monkeypatch.setattr(ep, "get_engine_provider", lambda: object())
    with pytest.raises(ValueError, match="requires a plan_input"):
        run_pipeline_activity({"repo_path": "/tmp/x", "plan_input": None})


def test_activity_runs_orchestrator_with_job_wiring(monkeypatch) -> None:
    import software_engineering_team.api.coding_team_main as main
    import software_engineering_team.engine_provider as ep

    monkeypatch.setattr(ep, "get_engine_provider", lambda: object())
    created: dict = {}
    monkeypatch.setattr(main, "create_job", lambda **kw: created.update(kw), raising=True)
    monkeypatch.setattr(main, "get_job", lambda jid: {"job_id": jid, "status": "completed"})

    captured: dict = {}

    # The activity mints a job, builds the plan, and delegates to the shared
    # ``run_orchestrator_wired``. Patch that seam on the (stable-identity) api.main
    # module — no ``coding_team.orchestrator`` sys.modules gymnastics needed.
    def _fake_wired(job_id, repo_path, plan, **kwargs):
        captured["args"] = (job_id, repo_path, plan)
        captured["kwargs"] = kwargs
        return None  # not paused -- falls through to get_job below

    monkeypatch.setattr(main, "run_orchestrator_wired", _fake_wired)

    out = run_pipeline_activity({"repo_path": "/repo", "plan_input": {"objective": "ship it"}})

    job_id, repo_path, plan = captured["args"]
    assert isinstance(job_id, str) and job_id  # a job id was minted
    assert repo_path == "/repo"
    # plan is a CodingTeamPlanInput carrying the repo_path merged in.
    assert getattr(plan, "repo_path", None) == "/repo"
    assert created["job_id"] == job_id
    # The activity returns a small fixed-shape terminal summary, not the full job record.
    assert out == {"outcome": "completed", "job_id": job_id, "status": "completed"}


def test_activity_reuses_supplied_job_id_and_skips_create_job(monkeypatch) -> None:
    """When the dispatcher (the API) already created the row and passes its
    job_id, the activity must reuse it — so the client polls the row the
    orchestrator writes — and must NOT create a second row."""
    import software_engineering_team.api.coding_team_main as main
    import software_engineering_team.engine_provider as ep

    monkeypatch.setattr(ep, "get_engine_provider", lambda: object())
    create_calls: list = []
    monkeypatch.setattr(main, "create_job", lambda **kw: create_calls.append(kw), raising=True)
    monkeypatch.setattr(main, "get_job", lambda jid: {"job_id": jid, "status": "completed"})

    captured: dict = {}

    def _fake_wired(job_id, repo_path, plan, **kwargs):
        captured["args"] = (job_id, repo_path, plan)
        return None

    monkeypatch.setattr(main, "run_orchestrator_wired", _fake_wired)

    out = run_pipeline_activity(
        {"job_id": "api-job-1", "repo_path": "/repo", "plan_input": {"objective": "ship it"}}
    )

    assert captured["args"][0] == "api-job-1"  # reused, not re-minted
    assert create_calls == []  # API owns creation; activity must not duplicate it
    assert out == {"outcome": "completed", "job_id": "api-job-1", "status": "completed"}


def test_plan_from_input_binds_request_repo_path_over_embedded() -> None:
    """The shared plan builder makes the request's repo_path authoritative — a
    repo_path embedded in the plan payload must not win."""
    import software_engineering_team.api.coding_team_main as main

    plan = main.plan_from_input({"objective": "x", "repo_path": "/embedded"}, "/authoritative")
    assert plan.repo_path == "/authoritative"


def test_run_orchestrator_wired_passes_standard_job_store_wiring(monkeypatch) -> None:
    """The shared helper is the single source of the (update_job_fn, get_job_fn,
    cache_dir) wiring — verify it forwards exactly that to the orchestrator."""
    import software_engineering_team.api.coding_team_main as main
    from software_engineering_team.models import CodingTeamPlanInput

    captured: dict = {}

    def _fake_orch(job_id, repo_path, plan, **kwargs):
        captured["args"] = (job_id, repo_path, plan)
        captured["kwargs"] = kwargs

    monkeypatch.setattr(main, "run_coding_team_orchestrator", _fake_orch)

    plan = CodingTeamPlanInput.model_validate({"objective": "x", "repo_path": "/repo"})
    main.run_orchestrator_wired("job-9", "/repo", plan)

    assert captured["args"] == ("job-9", "/repo", plan)
    kwargs = captured["kwargs"]
    assert callable(kwargs["update_job_fn"])  # closes over job_id → update_job
    assert kwargs["get_job_fn"] is main.get_job
    assert kwargs["cache_dir"] == main.DEFAULT_CACHE_DIR


def test_activity_returns_paused_promptly_without_blocking(monkeypatch) -> None:
    """The literal acceptance criterion for issue #3987: when the orchestrator
    reports a pause, the activity returns that discriminated result verbatim and
    promptly -- it must never fall through to a blocking call."""
    import software_engineering_team.api.coding_team_main as main
    import software_engineering_team.engine_provider as ep
    from software_engineering_team import hitl

    monkeypatch.setattr(ep, "get_engine_provider", lambda: object())
    monkeypatch.setattr(main, "create_job", lambda **kw: None)

    def _no_blocking_wait(*_a, **_kw):  # pragma: no cover - must not be called
        raise AssertionError("run_pipeline_activity must not block through a pause")

    monkeypatch.setattr(hitl, "wait_for_answers", _no_blocking_wait)

    paused_result = {
        "outcome": "paused",
        "job_id": "job-paused",
        "resume_token": "job-paused:abc123",
        "pause_kind": "entry",
        "pause_context": None,
        "pending_questions": [{"id": "q1", "question_text": "Which framework?"}],
    }

    def _fake_wired(job_id, repo_path, plan, **kwargs):
        assert kwargs["pause_strategy"] == "return"
        return paused_result

    monkeypatch.setattr(main, "run_orchestrator_wired", _fake_wired)

    # get_job must never be reached in the paused path -- fail the test if it is.
    def _no_get_job(*_a, **_kw):  # pragma: no cover
        raise AssertionError("run_pipeline_activity must return the paused result directly")

    monkeypatch.setattr(main, "get_job", _no_get_job)

    out = run_pipeline_activity(
        {"job_id": "job-paused", "repo_path": "/repo", "plan_input": {"objective": "ship it"}}
    )

    assert out == paused_result


def test_activity_returns_terminal_snapshot_when_not_paused(monkeypatch) -> None:
    """When the orchestrator returns None (terminal), the activity falls through
    to get_job and translates the record into the small fixed-shape terminal
    summary -- not the full record."""
    import software_engineering_team.api.coding_team_main as main
    import software_engineering_team.engine_provider as ep

    monkeypatch.setattr(ep, "get_engine_provider", lambda: object())
    monkeypatch.setattr(main, "create_job", lambda **kw: None)
    monkeypatch.setattr(main, "get_job", lambda jid: {"job_id": jid, "status": "completed"})

    def _fake_wired(job_id, repo_path, plan, **kwargs):
        return None

    monkeypatch.setattr(main, "run_orchestrator_wired", _fake_wired)

    out = run_pipeline_activity(
        {"job_id": "job-done", "repo_path": "/repo", "plan_input": {"objective": "ship it"}}
    )

    assert out == {"outcome": "completed", "job_id": "job-done", "status": "completed"}


@pytest.mark.parametrize(
    "status,expected_outcome",
    [
        ("completed", "completed"),
        ("completed_with_failures", "completed"),
        ("already_complete", "completed"),
        ("failed", "failed"),
        ("cancelled", "failed"),
    ],
)
def test_activity_outcome_matches_terminal_success_statuses(
    monkeypatch, status: str, expected_outcome: str
) -> None:
    """`outcome` is "completed" for every status in hitl.TERMINAL_SUCCESS_STATUSES
    and "failed" for every other terminal status -- the single source of truth
    both this activity and the rest of the codebase agree on."""
    import software_engineering_team.api.coding_team_main as main
    import software_engineering_team.engine_provider as ep

    monkeypatch.setattr(ep, "get_engine_provider", lambda: object())
    monkeypatch.setattr(main, "create_job", lambda **kw: None)
    monkeypatch.setattr(main, "get_job", lambda jid: {"job_id": jid, "status": status})
    monkeypatch.setattr(main, "run_orchestrator_wired", lambda *a, **kw: None)

    out = run_pipeline_activity(
        {"job_id": "job-1", "repo_path": "/repo", "plan_input": {"objective": "ship it"}}
    )

    assert out["outcome"] == expected_outcome
    assert out["status"] == status


def test_activity_includes_error_and_summary_only_when_present(monkeypatch) -> None:
    """`error`/`summary` are included only when the job record actually carries a
    truthy value for them -- omitted otherwise, keeping the shape genuinely small."""
    import software_engineering_team.api.coding_team_main as main
    import software_engineering_team.engine_provider as ep

    monkeypatch.setattr(ep, "get_engine_provider", lambda: object())
    monkeypatch.setattr(main, "create_job", lambda **kw: None)
    monkeypatch.setattr(
        main,
        "get_job",
        lambda jid: {
            "job_id": jid,
            "status": "failed",
            "error": "boom",
            "status_text": "review outage",
        },
    )
    monkeypatch.setattr(main, "run_orchestrator_wired", lambda *a, **kw: None)

    out = run_pipeline_activity(
        {"job_id": "job-1", "repo_path": "/repo", "plan_input": {"objective": "ship it"}}
    )

    assert out == {
        "outcome": "failed",
        "job_id": "job-1",
        "status": "failed",
        "error": "boom",
        "summary": "review outage",
    }


def test_activity_terminal_result_stays_small_regardless_of_job_record_size(monkeypatch) -> None:
    """A large task_graph_snapshot (or any other bulky job-record field) on the
    job record does not inflate the activity's terminal-state result -- only the
    fixed-shape summary fields appear."""
    import software_engineering_team.api.coding_team_main as main
    import software_engineering_team.engine_provider as ep

    monkeypatch.setattr(ep, "get_engine_provider", lambda: object())
    monkeypatch.setattr(main, "create_job", lambda **kw: None)
    large_snapshot = [{"id": f"t{i}", "status": "merged"} for i in range(5000)]
    monkeypatch.setattr(
        main,
        "get_job",
        lambda jid: {
            "job_id": jid,
            "status": "completed",
            "task_graph_snapshot": large_snapshot,
            "agent_task_map": {f"t{i}": "worker" for i in range(5000)},
        },
    )
    monkeypatch.setattr(main, "run_orchestrator_wired", lambda *a, **kw: None)

    out = run_pipeline_activity(
        {"job_id": "job-1", "repo_path": "/repo", "plan_input": {"objective": "ship it"}}
    )

    assert out == {"outcome": "completed", "job_id": "job-1", "status": "completed"}
    assert "task_graph_snapshot" not in out
    assert "agent_task_map" not in out


def test_activity_returns_unknown_outcome_when_job_missing(monkeypatch) -> None:
    """A job the store has nothing for (edge case, not expected in practice) still
    returns a well-formed fixed-shape dict rather than raising or returning None."""
    import software_engineering_team.api.coding_team_main as main
    import software_engineering_team.engine_provider as ep

    monkeypatch.setattr(ep, "get_engine_provider", lambda: object())
    monkeypatch.setattr(main, "create_job", lambda **kw: None)
    monkeypatch.setattr(main, "get_job", lambda jid: None)
    monkeypatch.setattr(main, "run_orchestrator_wired", lambda *a, **kw: None)

    out = run_pipeline_activity(
        {"job_id": "job-1", "repo_path": "/repo", "plan_input": {"objective": "ship it"}}
    )

    assert out == {"outcome": "unknown", "job_id": "job-1", "status": "unknown"}


def test_activity_forwards_acknowledged_resume_token_and_return_strategy(monkeypatch) -> None:
    """The activity always requests pause_strategy="return" and forwards
    req.acknowledged_resume_token unchanged into run_orchestrator_wired."""
    import software_engineering_team.api.coding_team_main as main
    import software_engineering_team.engine_provider as ep

    monkeypatch.setattr(ep, "get_engine_provider", lambda: object())
    monkeypatch.setattr(main, "create_job", lambda **kw: None)
    monkeypatch.setattr(main, "get_job", lambda jid: {"job_id": jid, "status": "completed"})

    captured: dict = {}

    def _fake_wired(job_id, repo_path, plan, **kwargs):
        captured["kwargs"] = kwargs
        return None

    monkeypatch.setattr(main, "run_orchestrator_wired", _fake_wired)

    run_pipeline_activity(
        {
            "job_id": "job-ack",
            "repo_path": "/repo",
            "plan_input": {"objective": "ship it"},
            "acknowledged_resume_token": "job-ack:tok-1",
        }
    )

    assert captured["kwargs"]["pause_strategy"] == "return"
    assert captured["kwargs"]["acknowledged_resume_token"] == "job-ack:tok-1"


def test_mark_coding_team_job_failed_activity_updates_store(monkeypatch) -> None:
    """Local mark-failed fallback must persist failed status without GitHub calls."""
    import software_engineering_team.api.coding_team_main as main
    from software_engineering_team.temporal.coding_team_workflow import (
        mark_coding_team_job_failed_activity,
    )

    updates: list[dict] = []
    monkeypatch.setattr(
        main,
        "update_job",
        lambda jid, **kw: updates.append({"job_id": jid, **kw}),
    )
    monkeypatch.setattr(
        main,
        "get_job",
        lambda jid: {"job_id": jid, "status": "failed", "error": "branch prep failed"},
    )

    out = mark_coding_team_job_failed_activity(
        {"job_id": "job-x", "error": "branch prep failed"}
    )

    assert updates[-1]["status"] == "failed"
    assert updates[-1]["error"] == "branch prep failed"
    assert out["status"] == "failed"


def test_mark_coding_team_job_cancelled_activity_updates_store(monkeypatch) -> None:
    """Cancel terminalize activity must persist cancelled status without an error write."""
    import software_engineering_team.api.coding_team_main as main
    from software_engineering_team.temporal.coding_team_workflow import (
        mark_coding_team_job_cancelled_activity,
    )

    updates: list[dict] = []
    monkeypatch.setattr(
        main,
        "update_job",
        lambda jid, **kw: updates.append({"job_id": jid, **kw}),
    )
    monkeypatch.setattr(
        main,
        "get_job",
        lambda jid: {"job_id": jid, "status": "cancelled"},
    )

    out = mark_coding_team_job_cancelled_activity({"job_id": "job-x"})

    assert updates[-1]["status"] == "cancelled"
    assert updates[-1]["status_text"] == "Cancelled by user"
    assert "error" not in updates[-1]
    assert out["status"] == "cancelled"


def test_github_pipeline_activity_defers_terminal_success(monkeypatch) -> None:
    """GitHub Temporal runs must keep the job non-terminal until publish.

    Thread-mode uses ``_defer_terminal_success`` so orchestrator ``completed``
    is remapped to ``running``/``publishing``. The Temporal activity must do
    the same when ``request`` carries a non-empty ``github`` block.
    """
    import software_engineering_team.api.coding_team_main as main
    import software_engineering_team.engine_provider as ep

    monkeypatch.setattr(ep, "get_engine_provider", lambda: object())
    monkeypatch.setattr(main, "create_job", lambda **kw: None)
    updates: list[dict] = []
    monkeypatch.setattr(
        main,
        "update_job",
        lambda jid, **kw: updates.append({"job_id": jid, **kw}),
    )
    monkeypatch.setattr(
        main,
        "get_job",
        lambda jid: {"job_id": jid, "status": "running", "phase": "publishing"},
    )

    def _fake_orch(job_id, repo_path, plan, **kwargs):
        kwargs["update_job_fn"](status="completed", phase="completed")
        return None

    monkeypatch.setattr(main, "run_coding_team_orchestrator", _fake_orch)

    out = run_pipeline_activity(
        {
            "job_id": "job-gh-1",
            "repo_path": "/repo",
            "plan_input": {"objective": "ship it"},
            "github": {
                "owner": "acme",
                "repo": "widgets",
                "issue_number": 9,
                "issue_title": "Fix",
                "base": "main",
                "integration_branch": "khala/issue-9",
            },
        }
    )

    assert updates, "orchestrator must have written through update_job_fn"
    assert updates[-1]["status"] == "running"
    assert updates[-1]["phase"] == "publishing"
    assert out["status"] == "running"
    assert out["outcome"] == "completed"


def test_non_github_pipeline_activity_still_writes_completed(monkeypatch) -> None:
    """Without GitHub metadata, terminal success must remain ``completed``."""
    import software_engineering_team.api.coding_team_main as main
    import software_engineering_team.engine_provider as ep

    monkeypatch.setattr(ep, "get_engine_provider", lambda: object())
    monkeypatch.setattr(main, "create_job", lambda **kw: None)
    updates: list[dict] = []
    monkeypatch.setattr(
        main,
        "update_job",
        lambda jid, **kw: updates.append({"job_id": jid, **kw}),
    )
    monkeypatch.setattr(main, "get_job", lambda jid: {"job_id": jid, "status": "completed"})

    def _fake_orch(job_id, repo_path, plan, **kwargs):
        kwargs["update_job_fn"](status="completed", phase="completed")
        return None

    monkeypatch.setattr(main, "run_coding_team_orchestrator", _fake_orch)

    out = run_pipeline_activity(
        {"job_id": "job-plain", "repo_path": "/repo", "plan_input": {"objective": "ship it"}}
    )

    assert updates[-1]["status"] == "completed"
    assert out["status"] == "completed"
