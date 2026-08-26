"""Tests for the deep-research Temporal activities + thread-mode job body.

Activities are invoked directly (no worker/sandbox); ``job_manager`` is the fake
job client and the orchestrator is stubbed so no LLM runs. These pin the same
retry-safe job-store contract as the main pipeline: activities never write
FAILED; terminal guards are status-aware (FAILED/missing raise at
prepare/finalize; a clean terminal short-circuits).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from temporalio.exceptions import ApplicationError

from sales_team import job_runner
from sales_team.models import (
    DeepResearchRequest,
    DeepResearchResult,
    IdealCustomerProfile,
    Prospect,
    ProspectDossier,
)
from sales_team.temporal import deep_research_activities as dra
from sales_team.temporal.phase_models import DeepResearchContext

_REQUEST = {
    "product_name": "ProductX",
    "value_proposition": "Save 20% on outbound time",
    "icp": {"industry": ["SaaS"]},
    "target_prospects": 10,
    "max_per_company": 2,
}
_COMPANY = Prospect(id="cmp_1", company_name="Acme Corp")
_PROSPECT = Prospect(id="prs_1", company_name="Acme Corp", contact_name="Jane")


def _dctx(job_id: str = "dr-1", **over) -> dict:
    req = DeepResearchRequest(
        product_name="ProductX",
        value_proposition="Save 20% on outbound time",
        icp=IdealCustomerProfile(industry=["SaaS"]),
        target_prospects=10,
        max_per_company=2,
    )
    base = dict(
        request=req,
        job_id=job_id,
        insights_ctx="INS",
        icp_json=req.icp.model_dump_json(),
        companies_requested=40,
    )
    base.update(over)
    return DeepResearchContext(**base).model_dump(mode="json")


def _dossier(prospect_id: str = "prs_1") -> ProspectDossier:
    return ProspectDossier(
        dossier_id="dsr_1",
        prospect_id=prospect_id,
        full_name="Jane",
        current_title="VP",
        current_company="Acme",
        executive_summary="s",
        confidence=0.9,
    )


@pytest.fixture
def orch_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    m = MagicMock()
    monkeypatch.setattr("sales_team.orchestrator.SalesPodOrchestrator", lambda config=None: m)
    return m


@pytest.fixture(autouse=True)
def _fake_jobs(monkeypatch: pytest.MonkeyPatch, fake_job_client) -> None:
    monkeypatch.setattr(job_runner, "job_manager", fake_job_client)


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def test_prepare_writes_running_and_precomputes(monkeypatch, fake_job_client):
    fake_job_client.create_job("dr-ok", status="pending")
    monkeypatch.setattr("sales_team.outcome_store.load_current_insights", lambda: None)
    monkeypatch.setattr("sales_team.learning_engine.format_insights_for_prompt", lambda i: "CTX")

    out = dra.prepare_deep_research_activity("dr-ok", _REQUEST)

    ctx = DeepResearchContext.model_validate(out)
    assert ctx.stopped is False
    assert ctx.insights_ctx == "CTX"
    assert ctx.companies_requested == 40  # min(100, max(40, 10))
    assert '"industry"' in ctx.icp_json
    assert fake_job_client.get_job("dr-ok")["status"] == "running"


def test_prepare_stops_when_clean_terminal(monkeypatch, fake_job_client):
    fake_job_client.create_job("dr-c", status="cancelled")
    monkeypatch.setattr("sales_team.outcome_store.load_current_insights", lambda: None)
    out = dra.prepare_deep_research_activity("dr-c", _REQUEST)
    assert out["stopped"] is True
    assert fake_job_client.get_job("dr-c")["status"] == "cancelled"


def test_prepare_raises_when_failed(fake_job_client):
    fake_job_client.create_job("dr-f", status="failed")
    with pytest.raises(ApplicationError):
        dra.prepare_deep_research_activity("dr-f", _REQUEST)


def test_prepare_raises_when_missing(fake_job_client):
    with pytest.raises(RuntimeError, match="not found"):
        dra.prepare_deep_research_activity("dr-ghost", _REQUEST)


def test_prepare_raises_non_retryable_on_invalid_request(fake_job_client):
    fake_job_client.create_job("dr-bad", status="pending")
    with pytest.raises(ApplicationError) as ei:
        dra.prepare_deep_research_activity("dr-bad", {"product_name": "P"})
    assert ei.value.non_retryable is True
    assert fake_job_client.get_job("dr-bad")["status"] == "pending"  # no FAILED write


@pytest.mark.parametrize(
    "status", ["missing", "pending", "failed", "cancelled", "interrupted", "completed"]
)
def test_prepare_invokes_terminal_guard_for_every_status(monkeypatch, fake_job_client, status):
    """Prepare must route every status category through the shared
    ``_terminal_guard`` rather than a hand-rolled inline check."""
    if status != "missing":
        fake_job_client.create_job("dr-1", status=status)
    monkeypatch.setattr("sales_team.outcome_store.load_current_insights", lambda: None)

    calls = []
    real_guard = dra._terminal_guard

    def spy(job_id, *, phase, missing_msg):
        calls.append((job_id, phase))
        return real_guard(job_id, phase=phase, missing_msg=missing_msg)

    monkeypatch.setattr(dra, "_terminal_guard", spy)

    if status in ("missing", "failed"):
        with pytest.raises((RuntimeError, ApplicationError)):
            dra.prepare_deep_research_activity("dr-1", _REQUEST)
    else:
        dra.prepare_deep_research_activity("dr-1", _REQUEST)

    assert calls == [("dr-1", "deep_research_prepare")]


# ---------------------------------------------------------------------------
# companies
# ---------------------------------------------------------------------------


def test_companies_returns_dicts(orch_mock, fake_job_client):
    fake_job_client.create_job("dr-1", status="running")
    orch_mock.prospector.prospect_companies.return_value = MagicMock(prospects=[_COMPANY])
    out = dra.companies_activity(_dctx())
    assert out == [_COMPANY.model_dump(mode="json")]


def test_companies_empty_when_terminal(orch_mock, fake_job_client):
    fake_job_client.create_job("dr-1", status="cancelled")
    assert dra.companies_activity(_dctx()) == []
    orch_mock.prospector.prospect_companies.assert_not_called()


def test_companies_reraises_on_error(orch_mock, fake_job_client):
    fake_job_client.create_job("dr-1", status="running")
    orch_mock.prospector.prospect_companies.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        dra.companies_activity(_dctx())
    assert fake_job_client.get_job("dr-1")["status"] == "running"  # not FAILED


# ---------------------------------------------------------------------------
# map_company_one
# ---------------------------------------------------------------------------


def test_map_company_one_serializes_pairs(orch_mock, fake_job_client):
    fake_job_client.create_job("dr-1", status="running")
    orch_mock.map_company_one.return_value = [(_PROSPECT, 0.8)]
    out = dra.map_company_one_activity(_dctx(), _COMPANY.model_dump(mode="json"))
    assert out == [{"prospect": _PROSPECT.model_dump(mode="json"), "confidence": 0.8}]


def test_map_company_one_skips_non_retryably_when_terminal(orch_mock, fake_job_client):
    fake_job_client.create_job("dr-1", status="cancelled")
    with pytest.raises(ApplicationError) as ei:
        dra.map_company_one_activity(_dctx(), _COMPANY.model_dump(mode="json"))
    assert ei.value.non_retryable is True
    orch_mock.map_company_one.assert_not_called()


def test_map_company_one_reraises_on_error(orch_mock, fake_job_client):
    fake_job_client.create_job("dr-1", status="running")
    orch_mock.map_company_one.side_effect = RuntimeError("map boom")
    with pytest.raises(RuntimeError, match="map boom"):
        dra.map_company_one_activity(_dctx(), _COMPANY.model_dump(mode="json"))


def test_map_company_one_propagates_application_error(orch_mock, fake_job_client):
    fake_job_client.create_job("dr-1", status="running")
    orch_mock.map_company_one.side_effect = ApplicationError("nope", non_retryable=True)
    with pytest.raises(ApplicationError) as ei:
        dra.map_company_one_activity(_dctx(), _COMPANY.model_dump(mode="json"))
    assert ei.value.non_retryable is True


# ---------------------------------------------------------------------------
# rank
# ---------------------------------------------------------------------------


def test_rank_returns_ranked_prospects(fake_job_client):
    fake_job_client.create_job("dr-1", status="running")
    mapped = [
        {
            "prospect": Prospect(id="p1", company_name="A", icp_match_score=0.9).model_dump(
                mode="json"
            ),
            "confidence": 0.9,
        },
        {
            "prospect": Prospect(id="p2", company_name="B", icp_match_score=0.5).model_dump(
                mode="json"
            ),
            "confidence": 0.5,
        },
    ]
    out = dra.rank_activity(_dctx(), mapped)
    assert [p["id"] for p in out] == ["p1", "p2"]  # ranked by score desc


def test_rank_assigns_ids_when_missing(fake_job_client):
    fake_job_client.create_job("dr-1", status="running")
    mapped = [{"prospect": Prospect(company_name="A").model_dump(mode="json"), "confidence": 0.9}]
    out = dra.rank_activity(_dctx(), mapped)
    assert out[0]["id"].startswith("prs_")


def test_rank_empty_when_terminal(fake_job_client):
    fake_job_client.create_job("dr-1", status="interrupted")
    assert dra.rank_activity(_dctx(), []) == []


# ---------------------------------------------------------------------------
# build_dossier_one
# ---------------------------------------------------------------------------


def test_build_dossier_one_returns_dict(orch_mock, fake_job_client):
    fake_job_client.create_job("dr-1", status="running")
    orch_mock.build_dossier_one.return_value = _dossier()
    out = dra.build_dossier_one_activity(_dctx(), _PROSPECT.model_dump(mode="json"))
    assert out["prospect_id"] == "prs_1" and out["dossier_id"] == "dsr_1"


def test_build_dossier_one_skips_non_retryably_when_terminal(orch_mock, fake_job_client):
    fake_job_client.create_job("dr-1", status="cancelled")
    with pytest.raises(ApplicationError) as ei:
        dra.build_dossier_one_activity(_dctx(), _PROSPECT.model_dump(mode="json"))
    assert ei.value.non_retryable is True
    orch_mock.build_dossier_one.assert_not_called()


def test_build_dossier_one_reraises_on_error(orch_mock, fake_job_client):
    fake_job_client.create_job("dr-1", status="running")
    orch_mock.build_dossier_one.side_effect = RuntimeError("dossier boom")
    with pytest.raises(RuntimeError, match="dossier boom"):
        dra.build_dossier_one_activity(_dctx(), _PROSPECT.model_dump(mode="json"))


def test_build_dossier_one_propagates_application_error(orch_mock, fake_job_client):
    fake_job_client.create_job("dr-1", status="running")
    orch_mock.build_dossier_one.side_effect = ApplicationError("nope", non_retryable=True)
    with pytest.raises(ApplicationError):
        dra.build_dossier_one_activity(_dctx(), _PROSPECT.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# finalize
# ---------------------------------------------------------------------------


def _stub_assemble(monkeypatch, result: DeepResearchResult, captured: dict) -> None:
    def _fake(**kwargs):
        captured.update(kwargs)
        return result

    monkeypatch.setattr("sales_team.orchestrator.assemble_and_persist_deep_research", _fake)


def test_finalize_writes_completed_with_result(monkeypatch, fake_job_client):
    fake_job_client.create_job("dr-1", status="running")
    result = DeepResearchResult(
        list_id="plst_1", product_name="ProductX", total_prospects=1, companies_represented=1
    )
    captured: dict = {}
    _stub_assemble(monkeypatch, result, captured)

    out = dra.finalize_deep_research_activity(
        _dctx(), [_PROSPECT.model_dump(mode="json")], [_dossier().model_dump(mode="json")], []
    )
    assert out == {"job_id": "dr-1"}
    job = fake_job_client.get_job("dr-1")
    assert job["status"] == "completed"
    assert job["result"]["list_id"] == "plst_1"
    # dossiers reconstructed + keyed by prospect_id for the assembler
    assert "prs_1" in captured["dossiers"]


def test_finalize_skips_completed_when_clean_terminal(monkeypatch, fake_job_client):
    _stub_assemble(
        monkeypatch,
        DeepResearchResult(product_name="P", total_prospects=0, companies_represented=0),
        {},
    )
    fake_job_client.create_job("dr-1", status="cancelled")
    out = dra.finalize_deep_research_activity(_dctx(), [], [], [])
    assert out == {"job_id": "dr-1"}
    assert fake_job_client.get_job("dr-1")["status"] == "cancelled"


def test_finalize_raises_when_failed(fake_job_client):
    fake_job_client.create_job("dr-1", status="failed")
    with pytest.raises(ApplicationError):
        dra.finalize_deep_research_activity(_dctx(), [], [], [])


def test_finalize_raises_when_missing(fake_job_client):
    with pytest.raises(RuntimeError, match="not found"):
        dra.finalize_deep_research_activity(_dctx(), [], [], [])


def test_finalize_skips_completed_when_cancel_lands_during(monkeypatch, fake_job_client):
    """A cancel landing after the entry guard but before the COMPLETED write is
    respected (second terminal check)."""
    _stub_assemble(
        monkeypatch,
        DeepResearchResult(product_name="P", total_prospects=0, companies_represented=0),
        {},
    )
    fake_job_client.create_job("dr-1", status="running")
    statuses = iter(["running", "cancelled"])
    monkeypatch.setattr(dra, "_job_status", lambda job_id: next(statuses))
    dra.finalize_deep_research_activity(_dctx(), [], [], [])
    assert fake_job_client.get_job("dr-1")["status"] == "running"  # COMPLETED not written


# ---------------------------------------------------------------------------
# thread-mode job body
# ---------------------------------------------------------------------------


def test_run_deep_research_job_completes(monkeypatch, fake_job_client):
    fake_job_client.create_job("dr-job", status="pending")
    result = DeepResearchResult(product_name="ProductX", total_prospects=0, companies_represented=0)
    stub = MagicMock()
    stub.deep_research_only.return_value = result
    monkeypatch.setattr("sales_team.job_runner.SalesPodOrchestrator", lambda config=None: stub)

    req = DeepResearchRequest(
        product_name="ProductX",
        value_proposition="Save 20% on outbound time",
        icp=IdealCustomerProfile(industry=["SaaS"]),
    )
    job_runner.run_deep_research_job("dr-job", req)
    assert fake_job_client.get_job("dr-job")["status"] == "completed"


def test_run_deep_research_job_marks_failed(monkeypatch, fake_job_client):
    fake_job_client.create_job("dr-job", status="pending")
    stub = MagicMock()
    stub.deep_research_only.side_effect = RuntimeError("deep boom")
    monkeypatch.setattr("sales_team.job_runner.SalesPodOrchestrator", lambda config=None: stub)
    req = DeepResearchRequest(
        product_name="ProductX",
        value_proposition="Save 20% on outbound time",
        icp=IdealCustomerProfile(industry=["SaaS"]),
    )
    job_runner.run_deep_research_job("dr-job", req)
    assert fake_job_client.get_job("dr-job")["status"] == "failed"


def test_run_deep_research_job_skips_missing(fake_job_client):
    req = DeepResearchRequest(
        product_name="ProductX",
        value_proposition="Save 20% on outbound time",
        icp=IdealCustomerProfile(industry=["SaaS"]),
    )
    job_runner.run_deep_research_job("nope", req)  # must not raise
    assert fake_job_client.get_job("nope") is None


def test_run_deep_research_job_preserves_cancel_during_run(monkeypatch, fake_job_client):
    """A cancel landing while the run executes is preserved (COMPLETED not
    written over the terminal status)."""
    fake_job_client.create_job("dr-job", status="pending")
    result = DeepResearchResult(product_name="ProductX", total_prospects=0, companies_represented=0)

    def _run_then_cancel(*_a, **_k):
        fake_job_client.update_job("dr-job", status="cancelled")
        return result

    stub = MagicMock()
    stub.deep_research_only.side_effect = _run_then_cancel
    monkeypatch.setattr("sales_team.job_runner.SalesPodOrchestrator", lambda config=None: stub)
    req = DeepResearchRequest(
        product_name="ProductX",
        value_proposition="Save 20% on outbound time",
        icp=IdealCustomerProfile(industry=["SaaS"]),
    )
    job_runner.run_deep_research_job("dr-job", req)
    assert fake_job_client.get_job("dr-job")["status"] == "cancelled"


def test_run_deep_research_job_skips_terminal(monkeypatch, fake_job_client):
    fake_job_client.create_job("dr-t", status="cancelled")
    stub = MagicMock()
    monkeypatch.setattr("sales_team.job_runner.SalesPodOrchestrator", lambda config=None: stub)
    req = DeepResearchRequest(
        product_name="ProductX",
        value_proposition="Save 20% on outbound time",
        icp=IdealCustomerProfile(industry=["SaaS"]),
    )
    job_runner.run_deep_research_job("dr-t", req)
    stub.deep_research_only.assert_not_called()
    assert fake_job_client.get_job("dr-t")["status"] == "cancelled"


def test_run_deep_research_job_heartbeats_during_long_run(monkeypatch, fake_job_client):
    """A run that outlasts the beat interval gets its heartbeat refreshed so the
    stale-job monitor cannot fail an in-flight deep-research job."""
    import threading

    fake_job_client.create_job("dr-hb", status="pending")
    monkeypatch.setattr(job_runner, "DEEP_RESEARCH_HEARTBEAT_INTERVAL_S", 0.01)

    beat_seen = threading.Event()
    real_heartbeat = fake_job_client.heartbeat

    def _spy_heartbeat(job_id):
        real_heartbeat(job_id)
        beat_seen.set()

    monkeypatch.setattr(fake_job_client, "heartbeat", _spy_heartbeat)

    result = DeepResearchResult(product_name="ProductX", total_prospects=0, companies_represented=0)

    def _wait_for_beat(*_a, **_k):
        # Block until the background beater has refreshed the heartbeat at least
        # once — deterministic, no fixed sleeps.
        assert beat_seen.wait(timeout=5.0), "heartbeat never fired during the run"
        return result

    stub = MagicMock()
    stub.deep_research_only.side_effect = _wait_for_beat
    monkeypatch.setattr("sales_team.job_runner.SalesPodOrchestrator", lambda config=None: stub)

    req = DeepResearchRequest(
        product_name="ProductX",
        value_proposition="Save 20% on outbound time",
        icp=IdealCustomerProfile(industry=["SaaS"]),
    )
    job_runner.run_deep_research_job("dr-hb", req)

    assert beat_seen.is_set()
    assert fake_job_client.get_job("dr-hb")["status"] == "completed"


def test_run_deep_research_job_heartbeat_error_is_swallowed(monkeypatch, fake_job_client):
    """A heartbeat that raises mid-run is routed to ``on_error`` (logged, not
    fatal); the beater keeps looping and the run still completes."""
    import threading

    fake_job_client.create_job("dr-hb-err", status="pending")
    monkeypatch.setattr(job_runner, "DEEP_RESEARCH_HEARTBEAT_INTERVAL_S", 0.01)

    def _boom_heartbeat(_job_id):
        raise RuntimeError("heartbeat endpoint down")

    monkeypatch.setattr(fake_job_client, "heartbeat", _boom_heartbeat)

    on_error_seen = threading.Event()
    real_warning = job_runner.logger.warning

    def _spy_warning(msg, *args, **kwargs):
        if "heartbeat failed" in str(msg):
            on_error_seen.set()
        return real_warning(msg, *args, **kwargs)

    monkeypatch.setattr(job_runner.logger, "warning", _spy_warning)

    result = DeepResearchResult(product_name="ProductX", total_prospects=0, companies_represented=0)

    def _wait_for_error(*_a, **_k):
        assert on_error_seen.wait(timeout=5.0), "on_error never fired for the failing beat"
        return result

    stub = MagicMock()
    stub.deep_research_only.side_effect = _wait_for_error
    monkeypatch.setattr("sales_team.job_runner.SalesPodOrchestrator", lambda config=None: stub)

    req = DeepResearchRequest(
        product_name="ProductX",
        value_proposition="Save 20% on outbound time",
        icp=IdealCustomerProfile(industry=["SaaS"]),
    )
    job_runner.run_deep_research_job("dr-hb-err", req)

    assert on_error_seen.is_set()
    assert fake_job_client.get_job("dr-hb-err")["status"] == "completed"


def test_run_context_round_trips():
    original = _dctx(job_id="dr-rt")
    assert DeepResearchContext.model_validate(original).model_dump(mode="json") == original
