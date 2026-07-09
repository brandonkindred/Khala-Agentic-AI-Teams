"""Tests for the fine-grained sales Temporal activities.

Each activity is invoked directly (no worker/sandbox). ``job_manager`` is the
fake job client; the orchestrator is stubbed so no LLM runs. These assert the
job-store bookkeeping contract (RUNNING / COMPLETED / FAILED / terminal guards)
and that per-prospect activities re-raise (rather than fail the job) on error.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sales_team import job_runner
from sales_team.models import (
    BANTScore,
    IdealCustomerProfile,
    MEDDICScore,
    Prospect,
    ProspectDossier,
    QualificationScore,
    SalesPipelineRequest,
)
from sales_team.temporal import activities as acts
from sales_team.temporal.phase_models import SalesRunContext

_REQUEST = {
    "product_name": "ProductX",
    "value_proposition": "Save 20% on outbound time",
    "icp": {"industry": ["SaaS"]},
}
_PROSPECT = Prospect(id="prs_1", company_name="Acme Corp")


def _ctx_dict(job_id: str = "job-1", **over) -> dict:
    base = dict(
        request=SalesPipelineRequest(
            product_name="ProductX",
            value_proposition="Save 20% on outbound time",
            icp=IdealCustomerProfile(industry=["SaaS"]),
        ),
        job_id=job_id,
        insights_ctx="INSIGHTS",
        insights_version=2,
        insights_total_outcomes=5,
    )
    base.update(over)
    return SalesRunContext(**base).model_dump(mode="json")


def _dumpable(payload: dict) -> MagicMock:
    """A stand-in result object whose ``model_dump(mode="json")`` yields payload."""
    m = MagicMock()
    m.model_dump.return_value = payload
    return m


def _score() -> QualificationScore:
    return QualificationScore(
        prospect=_PROSPECT,
        bant=BANTScore(budget=3, authority=3, need=3, timeline=3),
        meddic=MEDDICScore(),
        overall_score=0.7,
        value_creation_level=2,
        recommended_action="advance",
    )


@pytest.fixture
def orch_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch the orchestrator the activities construct with a MagicMock."""
    m = MagicMock()
    monkeypatch.setattr("sales_team.orchestrator.SalesPodOrchestrator", lambda config=None: m)
    return m


@pytest.fixture(autouse=True)
def _fake_jobs(monkeypatch: pytest.MonkeyPatch, fake_job_client) -> None:
    monkeypatch.setattr(job_runner, "job_manager", fake_job_client)


# ---------------------------------------------------------------------------
# sales_prepare
# ---------------------------------------------------------------------------


def test_prepare_writes_running_and_returns_ctx(monkeypatch, fake_job_client):
    fake_job_client.create_job("job-ok", status="pending")

    class _Ins:
        insights_version = 7
        total_outcomes_analyzed = 12

    monkeypatch.setattr("sales_team.outcome_store.load_current_insights", lambda: _Ins())
    monkeypatch.setattr("sales_team.learning_engine.format_insights_for_prompt", lambda ins: "CTX")

    out = acts.prepare_sales_pipeline_activity("job-ok", _REQUEST)

    assert out["stopped"] is False
    assert out["insights_ctx"] == "CTX"
    assert out["insights_version"] == 7 and out["insights_total_outcomes"] == 12
    assert fake_job_client.get_job("job-ok")["status"] == "running"


def test_prepare_stops_without_running_when_job_terminal(monkeypatch, fake_job_client):
    fake_job_client.create_job("job-cancelled", status="cancelled")
    monkeypatch.setattr("sales_team.outcome_store.load_current_insights", lambda: None)

    out = acts.prepare_sales_pipeline_activity("job-cancelled", _REQUEST)

    assert out["stopped"] is True
    # RUNNING must NOT be written over a terminal status
    assert fake_job_client.get_job("job-cancelled")["status"] == "cancelled"


def test_prepare_marks_failed_on_invalid_request(fake_job_client):
    from pydantic import ValidationError

    fake_job_client.create_job("job-bad", status="pending")
    with pytest.raises(ValidationError):
        acts.prepare_sales_pipeline_activity("job-bad", {"product_name": "P"})
    assert fake_job_client.get_job("job-bad")["status"] == "failed"


# ---------------------------------------------------------------------------
# sales_prospect
# ---------------------------------------------------------------------------


def test_prospect_returns_prospect_dicts(orch_mock, fake_job_client):
    fake_job_client.create_job("job-1", status="running")
    orch_mock._run_prospecting.return_value = [_PROSPECT]
    out = acts.prospect_activity(_ctx_dict())
    assert out == [_PROSPECT.model_dump(mode="json")]


def test_prospect_returns_empty_when_terminal(orch_mock, fake_job_client):
    fake_job_client.create_job("job-1", status="cancelled")
    out = acts.prospect_activity(_ctx_dict())
    assert out == []
    orch_mock._run_prospecting.assert_not_called()


def test_prospect_marks_failed_and_raises_on_error(orch_mock, fake_job_client):
    fake_job_client.create_job("job-1", status="running")
    orch_mock._run_prospecting.side_effect = RuntimeError("prospecting boom")
    with pytest.raises(RuntimeError, match="prospecting boom"):
        acts.prospect_activity(_ctx_dict())
    assert fake_job_client.get_job("job-1")["status"] == "failed"


# ---------------------------------------------------------------------------
# sales_load_dossiers
# ---------------------------------------------------------------------------


def test_load_dossiers_maps_by_id(orch_mock, fake_job_client):
    fake_job_client.create_job("job-1", status="running")
    dossier = ProspectDossier(
        dossier_id="d1",
        prospect_id="prs_1",
        full_name="J",
        current_title="VP",
        current_company="Acme",
        executive_summary="s",
        confidence=0.9,
    )
    orch_mock.load_dossiers_for_prospects.return_value = {"prs_1": dossier}
    out = acts.load_dossiers_activity(_ctx_dict(), [_PROSPECT.model_dump(mode="json")])
    assert out == {"prs_1": dossier.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# per-prospect activities
# ---------------------------------------------------------------------------


def test_qualify_one_returns_score_dict(orch_mock, fake_job_client):
    fake_job_client.create_job("job-1", status="running")
    orch_mock.qualify_one.return_value = _score()
    out = acts.qualify_one_activity(_ctx_dict(), _PROSPECT.model_dump(mode="json"))
    assert out["recommended_action"] == "advance"
    assert out["prospect"]["id"] == "prs_1"


def test_qualify_one_reraises_without_failing_job(orch_mock, fake_job_client):
    """A single prospect's failure must NOT fail the whole job — it re-raises so
    Temporal retries and the workflow drops just this prospect."""
    fake_job_client.create_job("job-1", status="running")
    orch_mock.qualify_one.side_effect = RuntimeError("one boom")
    with pytest.raises(RuntimeError, match="one boom"):
        acts.qualify_one_activity(_ctx_dict(), _PROSPECT.model_dump(mode="json"))
    # job stays running — a per-prospect failure is not terminal for the run
    assert fake_job_client.get_job("job-1")["status"] == "running"


def test_discovery_one_passes_optional_qual(orch_mock, fake_job_client):
    from sales_team.models import DiscoveryPlan, SPINQuestions

    fake_job_client.create_job("job-1", status="running")
    plan = DiscoveryPlan(prospect=_PROSPECT, spin_questions=SPINQuestions())
    orch_mock.discovery_one.return_value = plan
    out = acts.discovery_one_activity(_ctx_dict(), _PROSPECT.model_dump(mode="json"), None)
    assert out["prospect"]["id"] == "prs_1"
    # qual argument reconstructed as None (no qualification passed)
    assert orch_mock.discovery_one.call_args.args[1] is None


def test_proposal_one_reconstructs_dossier_and_qual(orch_mock, fake_job_client):
    fake_job_client.create_job("job-1", status="running")
    orch_mock.proposal_one.return_value = _dumpable({"prospect": {"id": "prs_1"}})
    dossier = ProspectDossier(
        dossier_id="d1",
        prospect_id="prs_1",
        full_name="J",
        current_title="VP",
        current_company="Acme",
        executive_summary="s",
        confidence=0.9,
    )
    out = acts.proposal_one_activity(
        _ctx_dict(),
        _PROSPECT.model_dump(mode="json"),
        dossier.model_dump(mode="json"),
        _score().model_dump(mode="json"),
    )
    assert out["prospect"]["id"] == "prs_1"
    # dossier + qual reconstructed into models before the call
    call = orch_mock.proposal_one.call_args
    assert isinstance(call.args[1], ProspectDossier)
    assert isinstance(call.args[2], QualificationScore)


# ---------------------------------------------------------------------------
# sales_coach
# ---------------------------------------------------------------------------


def test_coach_returns_report_or_none(orch_mock, fake_job_client):
    fake_job_client.create_job("job-1", status="running")
    orch_mock._run_coaching.return_value = _dumpable({"overall_health": "good"})
    out = acts.coach_activity(_ctx_dict(), [_PROSPECT.model_dump(mode="json")])
    assert out["overall_health"] == "good"

    orch_mock._run_coaching.return_value = None
    assert acts.coach_activity(_ctx_dict(), []) is None


# ---------------------------------------------------------------------------
# sales_report_progress
# ---------------------------------------------------------------------------


def test_report_progress_writes_and_reports_active(fake_job_client):
    fake_job_client.create_job("job-1", status="running")
    assert acts.report_progress_activity("job-1", "outreach", 20) is True
    job = fake_job_client.get_job("job-1")
    assert job["current_stage"] == "outreach" and job["progress"] == 20


def test_report_progress_false_when_terminal(fake_job_client):
    fake_job_client.create_job("job-1", status="cancelled")
    assert acts.report_progress_activity("job-1", "outreach", 20) is False


def test_report_progress_false_when_missing(fake_job_client):
    assert acts.report_progress_activity("nope", "outreach", 20) is False


# ---------------------------------------------------------------------------
# sales_finalize
# ---------------------------------------------------------------------------


def _result_dict(prospects: list) -> dict:
    return {
        "job_id": "job-1",
        "entry_stage": "prospecting",
        "product_name": "ProductX",
        "prospects": prospects,
    }


def test_finalize_no_prospects_writes_halted_summary(orch_mock, fake_job_client):
    fake_job_client.create_job("job-1", status="running")
    out = acts.finalize_sales_pipeline_activity(_ctx_dict(), _result_dict([]))
    assert out == {"job_id": "job-1"}
    job = fake_job_client.get_job("job-1")
    assert job["status"] == "completed"
    assert job["result"]["summary"] == "No prospects found or provided. Pipeline halted."
    orch_mock._record_prospecting_outcomes.assert_not_called()


def test_finalize_records_outcomes_and_completes(orch_mock, fake_job_client):
    fake_job_client.create_job("job-1", status="running")
    out = acts.finalize_sales_pipeline_activity(
        _ctx_dict(), _result_dict([_PROSPECT.model_dump(mode="json")])
    )
    assert out == {"job_id": "job-1"}
    job = fake_job_client.get_job("job-1")
    assert job["status"] == "completed"
    assert "Prospects identified: 1" in job["result"]["summary"]
    assert "learning insights v2 applied" in job["result"]["summary"]
    orch_mock._record_prospecting_outcomes.assert_called_once()


def test_finalize_skips_completed_when_terminal(orch_mock, fake_job_client):
    fake_job_client.create_job("job-1", status="cancelled")
    out = acts.finalize_sales_pipeline_activity(
        _ctx_dict(), _result_dict([_PROSPECT.model_dump(mode="json")])
    )
    assert out == {"job_id": "job-1"}
    # cancel wins — COMPLETED not written
    assert fake_job_client.get_job("job-1")["status"] == "cancelled"
    orch_mock._record_prospecting_outcomes.assert_not_called()


# ---------------------------------------------------------------------------
# serialization round-trips across the activity boundary
# ---------------------------------------------------------------------------


def _dossier_dict() -> dict:
    return ProspectDossier(
        dossier_id="d1",
        prospect_id="prs_1",
        full_name="J",
        current_title="VP",
        current_company="Acme",
        executive_summary="s",
        confidence=0.9,
    ).model_dump(mode="json")


def test_outreach_one_returns_sequence_dict(orch_mock, fake_job_client):
    fake_job_client.create_job("job-1", status="running")
    orch_mock.outreach_one.return_value = _dumpable({"prospect": {"id": "prs_1"}})
    out = acts.outreach_one_activity(
        _ctx_dict(), _PROSPECT.model_dump(mode="json"), _dossier_dict()
    )
    assert out == {"prospect": {"id": "prs_1"}}
    # dossier reconstructed into a model before the call
    assert isinstance(orch_mock.outreach_one.call_args.args[1], ProspectDossier)


def test_nurture_one_returns_sequence_dict(orch_mock, fake_job_client):
    fake_job_client.create_job("job-1", status="running")
    orch_mock.nurture_one.return_value = _dumpable({"prospect": {"id": "prs_1"}})
    out = acts.nurture_one_activity(_ctx_dict(), _PROSPECT.model_dump(mode="json"))
    assert out == {"prospect": {"id": "prs_1"}}


def test_close_one_returns_strategy_dict(orch_mock, fake_job_client):
    fake_job_client.create_job("job-1", status="running")
    orch_mock.close_one.return_value = _dumpable({"prospect": {"id": "prs_1"}})
    out = acts.close_one_activity(_ctx_dict(), _PROSPECT.model_dump(mode="json"), None)
    assert out == {"prospect": {"id": "prs_1"}}
    assert orch_mock.close_one.call_args.args[1] is None  # no proposal => None


@pytest.mark.parametrize(
    "activity_fn,extra",
    [
        ("outreach_one_activity", (_dossier_dict(),)),
        ("nurture_one_activity", ()),
        ("discovery_one_activity", (None,)),
        ("proposal_one_activity", (None, None)),
        ("close_one_activity", (None,)),
    ],
)
def test_per_prospect_activity_reraises_without_failing_job(
    orch_mock, fake_job_client, activity_fn, extra
):
    """Every per-prospect activity re-raises (so Temporal retries) and never
    marks the whole job FAILED for one prospect's error."""
    fake_job_client.create_job("job-1", status="running")
    method = activity_fn.replace("_activity", "")  # e.g. outreach_one_activity -> outreach_one
    getattr(orch_mock, method).side_effect = RuntimeError("kaboom")
    with pytest.raises(RuntimeError, match="kaboom"):
        getattr(acts, activity_fn)(_ctx_dict(), _PROSPECT.model_dump(mode="json"), *extra)
    assert fake_job_client.get_job("job-1")["status"] == "running"


def test_finalize_skips_completed_when_cancel_lands_during(monkeypatch, orch_mock, fake_job_client):
    """A cancel that lands after the entry guard but before the COMPLETED write
    is still respected (second terminal check)."""
    fake_job_client.create_job("job-1", status="running")
    monkeypatch.setattr(acts, "_job_is_terminal", MagicMock(side_effect=[False, True]))
    acts.finalize_sales_pipeline_activity(
        _ctx_dict(), _result_dict([_PROSPECT.model_dump(mode="json")])
    )
    assert fake_job_client.get_job("job-1")["status"] == "running"  # COMPLETED not written
    orch_mock._record_prospecting_outcomes.assert_called_once()  # outcomes still recorded


def test_finalize_marks_failed_and_raises_on_write_error(orch_mock, fake_job_client):
    """A genuine finalize failure (job-store write error) is marked FAILED and
    re-raised; the best-effort FAILED write swallows its own secondary error."""
    fake_job_client.create_job("job-1", status="running")

    def _boom(*_a, **_k):
        raise RuntimeError("store down")

    fake_job_client.update_job = _boom  # both COMPLETED and the FAILED fallback fail
    with pytest.raises(RuntimeError, match="store down"):
        acts.finalize_sales_pipeline_activity(
            _ctx_dict(), _result_dict([_PROSPECT.model_dump(mode="json")])
        )


def test_heartbeat_interval_parsing(monkeypatch):
    monkeypatch.setenv("SALES_TEMPORAL_HEARTBEAT_INTERVAL_S", "12.5")
    assert acts._heartbeat_interval_s() == 12.5
    monkeypatch.setenv("SALES_TEMPORAL_HEARTBEAT_INTERVAL_S", "garbage")
    assert acts._heartbeat_interval_s() == acts._DEFAULT_HEARTBEAT_INTERVAL_S
    monkeypatch.setenv("SALES_TEMPORAL_HEARTBEAT_INTERVAL_S", "-3")
    assert acts._heartbeat_interval_s() == acts._DEFAULT_HEARTBEAT_INTERVAL_S


def test_run_context_round_trips():
    original = _ctx_dict(job_id="job-rt")
    restored = SalesRunContext.model_validate(original)
    assert restored.model_dump(mode="json") == original


def test_dossier_round_trips():
    dossier = ProspectDossier(
        dossier_id="d1",
        prospect_id="prs_1",
        full_name="J",
        current_title="VP",
        current_company="Acme",
        executive_summary="s",
        confidence=0.9,
    )
    dumped = dossier.model_dump(mode="json")
    assert ProspectDossier.model_validate(dumped).model_dump(mode="json") == dumped
