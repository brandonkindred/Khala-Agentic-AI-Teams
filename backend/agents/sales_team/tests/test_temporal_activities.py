"""Tests for the fine-grained sales Temporal activities.

Each activity is invoked directly (no worker/sandbox). ``job_manager`` is the
fake job client; the orchestrator/module helpers are stubbed so no LLM runs.
These pin the retry-safe job-store contract: activities never write FAILED
themselves (only ``sales_mark_failed`` does, driven by the workflow), terminal
guards are status-aware (FAILED/missing raise at prepare/finalize; a clean
terminal short-circuits), and finalize does NOT re-validate the result.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from temporalio.exceptions import ApplicationError

from sales_team import job_runner
from sales_team.models import (
    BANTScore,
    IdealCustomerProfile,
    MEDDICScore,
    OutreachSequence,
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
# _terminal_guard
# ---------------------------------------------------------------------------


def test_terminal_guard_missing_job_raises_with_missing_msg_verbatim():
    with pytest.raises(RuntimeError, match="custom missing text"):
        acts._terminal_guard(
            "no-such-job", phase="sales_prepare", missing_msg="custom missing text"
        )


def test_terminal_guard_running_returns_proceed(fake_job_client):
    fake_job_client.create_job("job-1", status="running")
    result = acts._terminal_guard("job-1", phase="sales_prepare", missing_msg="unused")
    assert result is acts._GuardOutcome.PROCEED


def test_terminal_guard_failed_sales_prepare_message(fake_job_client):
    fake_job_client.create_job("job-1", status="failed")
    with pytest.raises(
        ApplicationError, match="Sales pipeline job job-1 was already FAILED before start"
    ) as exc_info:
        acts._terminal_guard("job-1", phase="sales_prepare", missing_msg="unused")
    assert exc_info.value.non_retryable is True


def test_terminal_guard_failed_sales_finalize_message(fake_job_client):
    fake_job_client.create_job("job-1", status="failed")
    with pytest.raises(
        ApplicationError, match="Sales pipeline job job-1 was marked FAILED during the run"
    ) as exc_info:
        acts._terminal_guard("job-1", phase="sales_finalize", missing_msg="unused")
    assert exc_info.value.non_retryable is True


def test_terminal_guard_failed_deep_research_prepare_message(fake_job_client):
    fake_job_client.create_job("job-1", status="failed")
    with pytest.raises(
        ApplicationError, match="Deep-research job job-1 was already FAILED before start"
    ) as exc_info:
        acts._terminal_guard("job-1", phase="deep_research_prepare", missing_msg="unused")
    assert exc_info.value.non_retryable is True


def test_terminal_guard_failed_deep_research_finalize_message(fake_job_client):
    fake_job_client.create_job("job-1", status="failed")
    with pytest.raises(
        ApplicationError, match="Deep-research job job-1 was marked FAILED during the run"
    ) as exc_info:
        acts._terminal_guard("job-1", phase="deep_research_finalize", missing_msg="unused")
    assert exc_info.value.non_retryable is True


def test_terminal_guard_cancelled_returns_stop(fake_job_client):
    fake_job_client.create_job("job-1", status="cancelled")
    result = acts._terminal_guard("job-1", phase="sales_finalize", missing_msg="unused")
    assert result is acts._GuardOutcome.STOP


def test_terminal_guard_interrupted_returns_stop(fake_job_client):
    fake_job_client.create_job("job-1", status="interrupted")
    result = acts._terminal_guard("job-1", phase="deep_research_finalize", missing_msg="unused")
    assert result is acts._GuardOutcome.STOP


def test_terminal_guard_completed_returns_stop(fake_job_client):
    fake_job_client.create_job("job-1", status="completed")
    result = acts._terminal_guard("job-1", phase="sales_prepare", missing_msg="unused")
    assert result is acts._GuardOutcome.STOP


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


def test_prepare_strips_existing_prospects_from_carrier(monkeypatch, fake_job_client):
    """The carrier must not embed supplied prospects — they flow as explicit
    activity args, and carrying up to 100 in every ctx bloats workflow history."""
    fake_job_client.create_job("job-strip", status="pending")
    monkeypatch.setattr("sales_team.outcome_store.load_current_insights", lambda: None)
    req = dict(_REQUEST, existing_prospects=[_PROSPECT.model_dump(mode="json")])

    out = acts.prepare_sales_pipeline_activity("job-strip", req)

    assert SalesRunContext.model_validate(out).request.existing_prospects == []


def test_prepare_stops_without_running_when_clean_terminal(monkeypatch, fake_job_client):
    fake_job_client.create_job("job-cancelled", status="cancelled")
    monkeypatch.setattr("sales_team.outcome_store.load_current_insights", lambda: None)

    out = acts.prepare_sales_pipeline_activity("job-cancelled", _REQUEST)

    assert out["stopped"] is True
    assert fake_job_client.get_job("job-cancelled")["status"] == "cancelled"


def test_prepare_raises_when_job_already_failed(fake_job_client):
    fake_job_client.create_job("job-failed", status="failed")
    with pytest.raises(ApplicationError):
        acts.prepare_sales_pipeline_activity("job-failed", _REQUEST)


def test_prepare_raises_when_job_missing(fake_job_client):
    with pytest.raises(RuntimeError, match="not found"):
        acts.prepare_sales_pipeline_activity("job-ghost", _REQUEST)


def test_prepare_raises_non_retryable_on_invalid_request(fake_job_client):
    """An invalid request is deterministic — retrying can't help — so prepare
    raises a non-retryable ApplicationError rather than writing FAILED itself
    (the workflow's catch-all records FAILED)."""
    fake_job_client.create_job("job-bad", status="pending")
    with pytest.raises(ApplicationError) as ei:
        acts.prepare_sales_pipeline_activity("job-bad", {"product_name": "P"})
    assert ei.value.non_retryable is True
    # prepare did NOT write FAILED (that would defeat retries and pre-empt mark_failed)
    assert fake_job_client.get_job("job-bad")["status"] == "pending"


# ---------------------------------------------------------------------------
# sales_prospect
# ---------------------------------------------------------------------------


def test_prospect_generates_when_no_existing(orch_mock, fake_job_client):
    fake_job_client.create_job("job-1", status="running")
    orch_mock._run_prospecting.return_value = [_PROSPECT]
    out = acts.prospect_activity(_ctx_dict(), [])
    assert out == [_PROSPECT.model_dump(mode="json")]


def test_prospect_adopts_existing_without_generating(orch_mock, fake_job_client):
    fake_job_client.create_job("job-1", status="running")
    out = acts.prospect_activity(_ctx_dict(), [_PROSPECT.model_dump(mode="json")])
    assert out[0]["id"] == "prs_1"
    orch_mock._run_prospecting.assert_not_called()


def test_prospect_returns_empty_when_terminal(orch_mock, fake_job_client):
    fake_job_client.create_job("job-1", status="cancelled")
    out = acts.prospect_activity(_ctx_dict(), [])
    assert out == []
    orch_mock._run_prospecting.assert_not_called()


def test_prospect_reraises_without_writing_failed(orch_mock, fake_job_client):
    """A transient prospecting failure must re-raise UNMARKED so Temporal's retry
    actually re-runs it; the job must not be pre-marked FAILED (which would trip
    the terminal guard on attempt 2 and defeat the retry)."""
    fake_job_client.create_job("job-1", status="running")
    orch_mock._run_prospecting.side_effect = RuntimeError("prospecting boom")
    with pytest.raises(RuntimeError, match="prospecting boom"):
        acts.prospect_activity(_ctx_dict(), [])
    assert fake_job_client.get_job("job-1")["status"] == "running"


# ---------------------------------------------------------------------------
# sales_load_dossiers (lean — no orchestrator construction)
# ---------------------------------------------------------------------------


def test_load_dossiers_maps_by_id(monkeypatch, fake_job_client):
    dossier = ProspectDossier(
        dossier_id="d1",
        prospect_id="prs_1",
        full_name="J",
        current_title="VP",
        current_company="Acme",
        executive_summary="s",
        confidence=0.9,
    )
    monkeypatch.setattr(
        "sales_team.orchestrator.load_dossiers_for_prospects", lambda prospects: {"prs_1": dossier}
    )
    out = acts.load_dossiers_activity([_PROSPECT.model_dump(mode="json")])
    assert out == {"prs_1": dossier.model_dump(mode="json")}


def test_load_dossiers_never_raises(monkeypatch, fake_job_client):
    def _boom(_p):
        raise RuntimeError("store down")

    monkeypatch.setattr("sales_team.orchestrator.load_dossiers_for_prospects", _boom)
    assert acts.load_dossiers_activity([_PROSPECT.model_dump(mode="json")]) == {}


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
    fake_job_client.create_job("job-1", status="running")
    orch_mock.qualify_one.side_effect = RuntimeError("one boom")
    with pytest.raises(RuntimeError, match="one boom"):
        acts.qualify_one_activity(_ctx_dict(), _PROSPECT.model_dump(mode="json"))
    assert fake_job_client.get_job("job-1")["status"] == "running"


def test_per_prospect_activity_skips_non_retryably_when_terminal(orch_mock, fake_job_client):
    """A cancel mid-fan-out short-circuits per-prospect work with a non-retryable
    error (stop LLM spend), and the orchestrator is never constructed."""
    fake_job_client.create_job("job-1", status="cancelled")
    with pytest.raises(ApplicationError) as ei:
        acts.qualify_one_activity(_ctx_dict(), _PROSPECT.model_dump(mode="json"))
    assert ei.value.non_retryable is True
    orch_mock.qualify_one.assert_not_called()


def test_per_prospect_activity_propagates_application_error(orch_mock, fake_job_client):
    """An ApplicationError raised by the stage method propagates unchanged (not
    re-wrapped by the generic handler), preserving its retry semantics."""
    fake_job_client.create_job("job-1", status="running")
    orch_mock.qualify_one.side_effect = ApplicationError("bad input", non_retryable=True)
    with pytest.raises(ApplicationError) as ei:
        acts.qualify_one_activity(_ctx_dict(), _PROSPECT.model_dump(mode="json"))
    assert ei.value.non_retryable is True


def test_outreach_one_reconstructs_dossier(orch_mock, fake_job_client):
    fake_job_client.create_job("job-1", status="running")
    orch_mock.outreach_one.return_value = _dumpable({"prospect": {"id": "prs_1"}})
    out = acts.outreach_one_activity(
        _ctx_dict(), _PROSPECT.model_dump(mode="json"), _dossier_dict()
    )
    assert out == {"prospect": {"id": "prs_1"}}
    assert isinstance(orch_mock.outreach_one.call_args.args[1], ProspectDossier)


def test_nurture_one_returns_dict(orch_mock, fake_job_client):
    fake_job_client.create_job("job-1", status="running")
    orch_mock.nurture_one.return_value = _dumpable({"prospect": {"id": "prs_1"}})
    assert acts.nurture_one_activity(_ctx_dict(), _PROSPECT.model_dump(mode="json")) == {
        "prospect": {"id": "prs_1"}
    }


def test_discovery_one_passes_optional_qual(orch_mock, fake_job_client):
    fake_job_client.create_job("job-1", status="running")
    orch_mock.discovery_one.return_value = _dumpable({"prospect": {"id": "prs_1"}})
    acts.discovery_one_activity(_ctx_dict(), _PROSPECT.model_dump(mode="json"), None)
    assert orch_mock.discovery_one.call_args.args[1] is None


def test_proposal_one_reconstructs_dossier_and_qual(orch_mock, fake_job_client):
    fake_job_client.create_job("job-1", status="running")
    orch_mock.proposal_one.return_value = _dumpable({"prospect": {"id": "prs_1"}})
    acts.proposal_one_activity(
        _ctx_dict(),
        _PROSPECT.model_dump(mode="json"),
        _dossier_dict(),
        _score().model_dump(mode="json"),
    )
    call = orch_mock.proposal_one.call_args
    assert isinstance(call.args[1], ProspectDossier)
    assert isinstance(call.args[2], QualificationScore)


def test_close_one_optional_proposal_none(orch_mock, fake_job_client):
    fake_job_client.create_job("job-1", status="running")
    orch_mock.close_one.return_value = _dumpable({"prospect": {"id": "prs_1"}})
    acts.close_one_activity(_ctx_dict(), _PROSPECT.model_dump(mode="json"), None)
    assert orch_mock.close_one.call_args.args[1] is None


# ---------------------------------------------------------------------------
# sales_coach (lean, best-effort)
# ---------------------------------------------------------------------------


def test_coach_returns_report_or_none(monkeypatch, fake_job_client):
    monkeypatch.setattr(
        "sales_team.orchestrator.coach_review",
        lambda prospects, product, insights: _dumpable({"overall_health": "good"}),
    )
    out = acts.coach_activity(_ctx_dict(), [_PROSPECT.model_dump(mode="json")])
    assert out["overall_health"] == "good"


def test_coach_absorbs_any_error(monkeypatch, fake_job_client):
    def _boom(*_a, **_k):
        raise RuntimeError("coach exploded")

    monkeypatch.setattr("sales_team.orchestrator.coach_review", _boom)
    assert acts.coach_activity(_ctx_dict(), [_PROSPECT.model_dump(mode="json")]) is None


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
# sales_mark_failed (the single writer of FAILED)
# ---------------------------------------------------------------------------


def test_mark_failed_writes_failed_for_active_job(fake_job_client):
    fake_job_client.create_job("job-1", status="running")
    acts.mark_failed_activity("job-1", "kaboom")
    job = fake_job_client.get_job("job-1")
    assert job["status"] == "failed" and "kaboom" in (job.get("error") or "")


def test_mark_failed_leaves_terminal_untouched(fake_job_client):
    fake_job_client.create_job("job-1", status="cancelled")
    acts.mark_failed_activity("job-1", "kaboom")
    assert fake_job_client.get_job("job-1")["status"] == "cancelled"


def test_mark_failed_noop_when_missing(fake_job_client):
    acts.mark_failed_activity("nope", "kaboom")  # must not raise


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


def test_finalize_no_prospects_writes_halted_summary(monkeypatch, fake_job_client):
    recorded = []
    monkeypatch.setattr(
        "sales_team.orchestrator.record_prospecting_outcomes",
        lambda prospects, job_id: recorded.append(job_id),
    )
    fake_job_client.create_job("job-1", status="running")
    out = acts.finalize_sales_pipeline_activity(_ctx_dict(), _result_dict([]))
    assert out == {"job_id": "job-1"}
    job = fake_job_client.get_job("job-1")
    assert job["status"] == "completed"
    assert job["result"]["summary"] == "No prospects found or provided. Pipeline halted."
    assert recorded == []  # no prospects → no outcomes


def test_finalize_records_outcomes_and_completes(monkeypatch, fake_job_client):
    recorded = []
    monkeypatch.setattr(
        "sales_team.orchestrator.record_prospecting_outcomes",
        lambda prospects, job_id: recorded.append((len(prospects), job_id)),
    )
    fake_job_client.create_job("job-1", status="running")
    out = acts.finalize_sales_pipeline_activity(
        _ctx_dict(), _result_dict([_PROSPECT.model_dump(mode="json")])
    )
    assert out == {"job_id": "job-1"}
    job = fake_job_client.get_job("job-1")
    assert job["status"] == "completed"
    assert "Prospects identified: 1" in job["result"]["summary"]
    assert "learning insights v2 applied" in job["result"]["summary"]
    assert recorded == [(1, "job-1")]


def test_finalize_preserves_variants_without_rerunning_confidence_gate(
    monkeypatch, fake_job_client
):
    """Regression: finalize must NOT re-validate the result. Re-running the
    OutreachSequence confidence gate without the request's threshold context
    would silently strip already-approved personalized variants."""
    monkeypatch.setattr(
        "sales_team.orchestrator.record_prospecting_outcomes", lambda prospects, job_id: None
    )
    fake_job_client.create_job("job-1", status="running")
    # A trigger_event variant survives at a 0.3 override but would be dropped by
    # the default-0.6 gate (dossier confidence 0.4).
    seq = OutreachSequence.model_validate(
        {
            "prospect": _PROSPECT,
            "dossier_id": "d1",
            "dossier_confidence": 0.4,
            "variants": [
                {
                    "angle": "trigger_event",
                    "email_sequence": [
                        {
                            "day": 1,
                            "subject_line": "Quick question for Acme",
                            "body": "x" * 40,
                            "call_to_action": "Open to a chat next week?",
                        }
                    ],
                    "rationale": "r",
                    "personalization_grade": "high",
                }
            ],
        },
        context={"dossier_confidence_threshold": 0.3},
    )
    assert len(seq.variants) == 1  # survived construction at the low threshold
    result = _result_dict([_PROSPECT.model_dump(mode="json")])
    result["outreach_sequences"] = [seq.model_dump(mode="json")]

    acts.finalize_sales_pipeline_activity(_ctx_dict(), result)

    written = fake_job_client.get_job("job-1")["result"]["outreach_sequences"][0]
    assert [v["angle"] for v in written["variants"]] == ["trigger_event"]


def test_finalize_skips_completed_when_clean_terminal(monkeypatch, fake_job_client):
    monkeypatch.setattr("sales_team.orchestrator.record_prospecting_outcomes", lambda p, j: None)
    fake_job_client.create_job("job-1", status="cancelled")
    out = acts.finalize_sales_pipeline_activity(
        _ctx_dict(), _result_dict([_PROSPECT.model_dump(mode="json")])
    )
    assert out == {"job_id": "job-1"}
    assert fake_job_client.get_job("job-1")["status"] == "cancelled"


def test_finalize_raises_when_job_failed(fake_job_client):
    """A FAILED job at finalize must surface as a failed workflow, never be
    masked as a clean success."""
    fake_job_client.create_job("job-1", status="failed")
    with pytest.raises(ApplicationError):
        acts.finalize_sales_pipeline_activity(_ctx_dict(), _result_dict([]))


def test_finalize_raises_when_job_missing(fake_job_client):
    with pytest.raises(RuntimeError, match="not found"):
        acts.finalize_sales_pipeline_activity(_ctx_dict(), _result_dict([]))


def test_finalize_skips_completed_when_cancel_lands_during(monkeypatch, fake_job_client):
    """A cancel that lands after the entry guard but before the COMPLETED write
    is still respected (second terminal check)."""
    monkeypatch.setattr("sales_team.orchestrator.record_prospecting_outcomes", lambda p, j: None)
    fake_job_client.create_job("job-1", status="running")
    statuses = iter(["running", "cancelled"])
    monkeypatch.setattr(acts, "_job_status", lambda job_id: next(statuses))
    acts.finalize_sales_pipeline_activity(
        _ctx_dict(), _result_dict([_PROSPECT.model_dump(mode="json")])
    )
    assert fake_job_client.get_job("job-1")["status"] == "running"  # COMPLETED not written


def test_finalize_invokes_terminal_guard_at_both_checkpoints(monkeypatch, fake_job_client):
    """Finalize must route both the pre- and post-summary terminal checks
    through ``_terminal_guard`` rather than a private closure."""
    monkeypatch.setattr("sales_team.orchestrator.record_prospecting_outcomes", lambda p, j: None)
    fake_job_client.create_job("job-1", status="running")

    calls = []
    real_guard = acts._terminal_guard

    def spy(job_id, *, phase, missing_msg):
        calls.append((job_id, phase))
        return real_guard(job_id, phase=phase, missing_msg=missing_msg)

    monkeypatch.setattr(acts, "_terminal_guard", spy)

    acts.finalize_sales_pipeline_activity(
        _ctx_dict(), _result_dict([_PROSPECT.model_dump(mode="json")])
    )

    assert calls == [("job-1", "sales_finalize"), ("job-1", "sales_finalize")]


# ---------------------------------------------------------------------------
# heartbeat interval clamping
# ---------------------------------------------------------------------------


def test_heartbeat_interval_clamped_below_timeout(monkeypatch):
    ceiling = acts.HEARTBEAT_TIMEOUT_S / 3.0
    monkeypatch.setenv("SALES_TEMPORAL_HEARTBEAT_INTERVAL_S", "12.5")
    assert acts._heartbeat_interval_s() == 12.5
    monkeypatch.setenv("SALES_TEMPORAL_HEARTBEAT_INTERVAL_S", "9999")
    assert acts._heartbeat_interval_s() == ceiling  # never exceeds a third of the timeout
    monkeypatch.setenv("SALES_TEMPORAL_HEARTBEAT_INTERVAL_S", "garbage")
    assert acts._heartbeat_interval_s() == acts._DEFAULT_HEARTBEAT_INTERVAL_S
    monkeypatch.setenv("SALES_TEMPORAL_HEARTBEAT_INTERVAL_S", "-3")
    assert acts._heartbeat_interval_s() == 1.0  # clamped up to floor


# ---------------------------------------------------------------------------
# serialization round-trips across the activity boundary
# ---------------------------------------------------------------------------


def test_run_pipeline_job_skips_missing_job(fake_job_client):
    """The thread-path body no-ops (never constructs the orchestrator) when the
    job row is absent — a queued run for a deleted job does nothing."""
    req = SalesPipelineRequest(
        product_name="ProductX",
        value_proposition="Save 20% on outbound time",
        icp=IdealCustomerProfile(industry=["SaaS"]),
    )
    job_runner.run_pipeline_job("ghost", req)  # must not raise
    assert fake_job_client.get_job("ghost") is None


def test_run_context_round_trips():
    original = _ctx_dict(job_id="job-rt")
    restored = SalesRunContext.model_validate(original)
    assert restored.model_dump(mode="json") == original


def test_dossier_round_trips():
    dumped = _dossier_dict()
    assert ProspectDossier.model_validate(dumped).model_dump(mode="json") == dumped
