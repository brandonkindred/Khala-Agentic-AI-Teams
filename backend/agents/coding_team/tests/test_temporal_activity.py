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
    import coding_team.orchestrator as orch

    monkeypatch.setattr(ep, "get_engine_provider", lambda: object())
    created: dict = {}
    monkeypatch.setattr(main, "create_job", lambda **kw: created.update(kw), raising=True)
    monkeypatch.setattr(main, "get_job", lambda jid: {"job_id": jid, "status": "completed"})
    monkeypatch.setattr(main, "update_job", lambda *a, **k: None)

    captured: dict = {}

    def _fake_orch(job_id, repo_path, plan, **kwargs):
        captured["args"] = (job_id, repo_path, plan)
        captured["kwargs"] = kwargs
        return None

    monkeypatch.setattr(orch, "run_coding_team_orchestrator", _fake_orch)

    out = run_pipeline_activity({"repo_path": "/repo", "plan_input": {"objective": "ship it"}})

    job_id, repo_path, plan = captured["args"]
    assert isinstance(job_id, str) and job_id  # a job id was minted
    assert repo_path == "/repo"
    # plan is a CodingTeamPlanInput carrying the repo_path merged in.
    assert getattr(plan, "repo_path", None) == "/repo"
    # The orchestrator is wired to the job store, not left to a file fallback.
    assert callable(captured["kwargs"].get("update_job_fn"))
    assert captured["kwargs"].get("get_job_fn") is main.get_job
    assert created["job_id"] == job_id
    # The activity returns the final job snapshot.
    assert out == {"job_id": job_id, "status": "completed"}
