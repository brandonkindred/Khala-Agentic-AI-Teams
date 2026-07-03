"""The coding_team Temporal activity wires the orchestrator with a real job.

Guards the seam that a stale positional call broke: the activity must fail fast
and clearly when mis-wired (no provider / no plan), and otherwise call the
orchestrator against its real ``(job_id, repo_path, plan)`` signature.
"""

from __future__ import annotations

import pytest

from coding_team.temporal import run_pipeline_activity


def test_activity_raises_without_provider(monkeypatch) -> None:
    import coding_team.engine_provider as ep

    monkeypatch.setattr(ep, "get_engine_provider", lambda: None)
    with pytest.raises(RuntimeError, match="no CodeEngineProvider"):
        run_pipeline_activity({"repo_path": "/tmp/x", "plan_input": {"objective": "o"}})


def test_activity_raises_without_plan(monkeypatch) -> None:
    import coding_team.engine_provider as ep

    monkeypatch.setattr(ep, "get_engine_provider", lambda: object())
    with pytest.raises(ValueError, match="requires a plan_input"):
        run_pipeline_activity({"repo_path": "/tmp/x", "plan_input": None})


def test_activity_runs_orchestrator_with_job_wiring(monkeypatch) -> None:
    import coding_team.api.main as main
    import coding_team.engine_provider as ep

    monkeypatch.setattr(ep, "get_engine_provider", lambda: object())
    created: dict = {}
    monkeypatch.setattr(main, "create_job", lambda **kw: created.update(kw), raising=True)
    monkeypatch.setattr(main, "get_job", lambda jid: {"job_id": jid, "status": "completed"})

    captured: dict = {}

    # The activity mints a job, builds the plan, and delegates to the shared
    # ``run_orchestrator_wired``. Patch that seam on the (stable-identity) api.main
    # module — no ``coding_team.orchestrator`` sys.modules gymnastics needed.
    def _fake_wired(job_id, repo_path, plan):
        captured["args"] = (job_id, repo_path, plan)

    monkeypatch.setattr(main, "run_orchestrator_wired", _fake_wired)

    out = run_pipeline_activity({"repo_path": "/repo", "plan_input": {"objective": "ship it"}})

    job_id, repo_path, plan = captured["args"]
    assert isinstance(job_id, str) and job_id  # a job id was minted
    assert repo_path == "/repo"
    # plan is a CodingTeamPlanInput carrying the repo_path merged in.
    assert getattr(plan, "repo_path", None) == "/repo"
    assert created["job_id"] == job_id
    # The activity returns the final job snapshot.
    assert out == {"job_id": job_id, "status": "completed"}


def test_run_orchestrator_wired_passes_standard_job_store_wiring(monkeypatch) -> None:
    """The shared helper is the single source of the (update_job_fn, get_job_fn,
    cache_dir) wiring — verify it forwards exactly that to the orchestrator."""
    import coding_team.api.main as main
    from coding_team.models import CodingTeamPlanInput

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
