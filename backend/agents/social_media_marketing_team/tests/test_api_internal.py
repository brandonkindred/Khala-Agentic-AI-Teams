from pathlib import Path

import pytest
from fastapi import HTTPException

from shared_job_management import CentralJobManager
from social_media_marketing_team.api import main as api_main


def _seed_job(job_id: str, request: api_main.RunMarketingTeamRequest) -> None:
    api_main._job_manager.create_job(
        job_id,
        status="pending",
        current_stage="queued",
        progress=0,
        llm_model_name=request.llm_model_name,
        brand_guidelines_path=request.brand_guidelines_path,
        brand_objectives_path=request.brand_objectives_path,
        result=None,
        error=None,
        eta_hint="queued",
        performance_observations=[],
        last_updated_at=api_main._now(),
        revision_history=[],
        request_payload=request.model_dump(),
    )


@pytest.fixture
def temp_job_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manager = CentralJobManager(team="social_media_marketing_team_test", cache_dir=tmp_path / "cache")
    monkeypatch.setattr(api_main, "_job_manager", manager)
    return manager


def test_read_text_file_and_update_job(tmp_path: Path, temp_job_manager: CentralJobManager) -> None:
    file_path = tmp_path / "doc.txt"
    file_path.write_text("hello")
    assert api_main._read_text_file(str(file_path)) == "hello"

    with pytest.raises(ValueError):
        api_main._read_text_file(str(tmp_path / "missing.txt"))

    api_main._update_job("missing", status="running")
    assert temp_job_manager.get_job("missing") is not None

    req = api_main.RunMarketingTeamRequest(
        brand_guidelines_path=str(file_path),
        brand_objectives_path=str(file_path),
        llm_model_name="x",
    )
    _seed_job("job-1", req)
    old_ts = temp_job_manager.get_job("job-1")["last_heartbeat_at"]
    api_main._update_job("job-1", status="running")
    job = temp_job_manager.get_job("job-1")
    assert job["status"] == "running"
    assert job["last_heartbeat_at"] >= old_ts


def test_run_team_job_success_and_failure(tmp_path: Path, temp_job_manager: CentralJobManager) -> None:
    guidelines = tmp_path / "guidelines.md"
    objectives = tmp_path / "objectives.md"
    guidelines.write_text("Keep tone direct")
    objectives.write_text("Grow followers")

    req = api_main.RunMarketingTeamRequest(
        brand_guidelines_path=str(guidelines),
        brand_objectives_path=str(objectives),
        llm_model_name="llama3.1",
        brand_name="Acme",
        target_audience="operators",
        human_approved_for_testing=True,
    )
    _seed_job("ok", req)

    api_main._run_team_job("ok", req)
    ok_job = temp_job_manager.get_job("ok")
    assert ok_job["status"] == "completed"
    assert ok_job["result"]["llm_model_name"] == "llama3.1"

    bad_req = api_main.RunMarketingTeamRequest(
        brand_guidelines_path=str(tmp_path / "missing-guidelines.md"),
        brand_objectives_path=str(objectives),
        llm_model_name="llama3.1",
    )
    _seed_job("bad", bad_req)
    api_main._run_team_job("bad", bad_req)
    bad_job = temp_job_manager.get_job("bad")
    assert bad_job["status"] == "failed"
    assert bad_job["error"]


def test_run_and_status_functions_with_inline_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    temp_job_manager: CentralJobManager,
) -> None:
    guidelines = tmp_path / "guidelines.md"
    objectives = tmp_path / "objectives.md"
    guidelines.write_text("Voice guide")
    objectives.write_text("Objective guide")

    class InlineThread:
        def __init__(self, target, args, daemon):
            self._target = target
            self._args = args
            self.daemon = daemon

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(api_main.threading, "Thread", InlineThread)

    request = api_main.RunMarketingTeamRequest(
        brand_guidelines_path=str(guidelines),
        brand_objectives_path=str(objectives),
        llm_model_name="mistral",
        human_approved_for_testing=False,
    )
    response = api_main.run_marketing_team(request)
    assert response.status == "running"

    status = api_main.get_marketing_job_status(response.job_id)
    assert status.status == "completed"
    assert status.result is not None

    perf = api_main.ingest_performance(response.job_id, api_main.PerformanceIngestRequest(observations=[]))
    assert perf.observations_ingested == 0

    revise = api_main.revise_marketing_team(
        response.job_id,
        api_main.ReviseMarketingTeamRequest(feedback="Add more detail", approved_for_testing=True),
    )
    assert revise.status == "running"

    with pytest.raises(HTTPException):
        api_main.get_marketing_job_status("missing-job-id")


def test_run_marketing_team_validation_and_health(temp_job_manager: CentralJobManager) -> None:
    with pytest.raises(HTTPException):
        api_main.run_marketing_team(
            api_main.RunMarketingTeamRequest(
                brand_guidelines_path="/tmp/nope-guidelines.md",
                brand_objectives_path="/tmp/nope-objectives.md",
                llm_model_name="model",
            )
        )

    with pytest.raises(HTTPException):
        api_main.revise_marketing_team(
            "missing",
            api_main.ReviseMarketingTeamRequest(feedback="retry", approved_for_testing=False),
        )

    assert api_main.health() == {"status": "ok"}
