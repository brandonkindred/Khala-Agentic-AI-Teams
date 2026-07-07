"""Tests for ``sales_team.api.main``.

These cover the FastAPI surface that previously had 0% coverage. Each test
patches the orchestrator and job manager so we exercise the route logic
without any LLM, Postgres, or Temporal dependency.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from sales_team import job_runner
from sales_team.api import main as api_main
from sales_team.models import (
    BANTScore,
    CloseType,
    ClosingStrategy,
    DealResult,
    DeepResearchResult,
    DiscoveryPlan,
    LearningInsights,
    MEDDICScore,
    NurtureSequence,
    OutcomeResult,
    OutreachSequence,
    OutreachVariant,
    PipelineCoachingReport,
    PipelineStage,
    Prospect,
    ProspectDossier,
    QualificationScore,
    ROIModel,
    SalesPipelineResult,
    SalesProposal,
    SPINQuestions,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, fake_job_client) -> TestClient:
    """Bind the FakeJobServiceClient and isolate outcome store paths."""
    monkeypatch.setattr(api_main, "_job_manager", fake_job_client)
    return TestClient(api_main.app)


@pytest.fixture(autouse=True)
def _isolate_outcome_store(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Point the outcome store at a tmpdir so list/insights endpoints don't
    leak between tests."""
    from sales_team import outcome_store

    cache_root = tmp_path / "outcomes"
    insights = tmp_path / "insights" / "current.json"
    monkeypatch.setattr(outcome_store, "_CACHE_ROOT", cache_root)
    monkeypatch.setattr(outcome_store, "_INSIGHTS_PATH", insights)


@pytest.fixture
def sample_prospect() -> Prospect:
    return Prospect(
        id="prs_api_test",
        company_name="Acme Corp",
        contact_name="Jane Smith",
        contact_title="VP Sales",
        icp_match_score=0.85,
    )


@pytest.fixture
def sample_icp_payload() -> dict:
    return {
        "industry": ["SaaS"],
        "company_size_min": 50,
        "company_size_max": 500,
        "job_titles": ["VP Sales"],
        "pain_points": ["manual reporting"],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_now_returns_iso_utc_string() -> None:
    out = api_main._now()
    assert "T" in out
    assert out.endswith("+00:00") or out.endswith("Z")


def test_update_job_delegates_to_job_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _Stub:
        def update_job(self, job_id: str, **fields: Any) -> None:
            captured["job_id"] = job_id
            captured["fields"] = fields

    monkeypatch.setattr(api_main, "_job_manager", _Stub())
    api_main._update_job("j1", status="running", progress=12)
    assert captured == {"job_id": "j1", "fields": {"status": "running", "progress": 12}}


def test_mark_all_running_jobs_failed_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _Stub:
        def mark_stale_active_jobs_failed(self, *, stale_after_seconds: float, reason: str) -> None:
            captured["stale"] = stale_after_seconds
            captured["reason"] = reason

    monkeypatch.setattr(api_main, "_job_manager", _Stub())
    api_main.mark_all_running_jobs_failed("server shutdown")
    assert captured == {"stale": 0, "reason": "server shutdown"}


# ---------------------------------------------------------------------------
# /sales/pipeline/run + background runner
# ---------------------------------------------------------------------------


def test_run_pipeline_creates_pending_job_and_starts_thread(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_job_client, sample_icp_payload
) -> None:
    """Stub the orchestrator so the background thread completes deterministically."""

    # Override threading.Thread with a synchronous stand-in that runs the
    # target inline on .start(). This lets us assert the post-completion job
    # state without sleeps or polling.
    class _InlineThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):  # noqa: ANN001
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(api_main.threading, "Thread", _InlineThread)

    class _StubOrch:
        def __init__(self, **_kw):
            pass

        def run(self, request, job_id, update_cb=None):
            update_cb("prospecting", 50)
            return SalesPipelineResult(
                job_id=job_id, entry_stage=request.entry_stage, product_name=request.product_name
            )

    # The thread target is job_runner.run_pipeline_job (imported into
    # api_main as _run_pipeline_job), so its orchestrator + job manager
    # references live in job_runner's own module namespace, not api_main's.
    monkeypatch.setattr(job_runner, "SalesPodOrchestrator", _StubOrch)
    monkeypatch.setattr(job_runner, "job_manager", fake_job_client)

    response = client.post(
        "/sales/pipeline/run",
        json={
            "product_name": "ProductX",
            "value_proposition": "Save 20% on outbound time",
            "icp": sample_icp_payload,
            "entry_stage": "prospecting",
            "max_prospects": 5,
            "existing_prospects": [],
            "company_context": "ctx",
            "case_study_snippets": [],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert "job_id" in body
    job_id = body["job_id"]

    # The fake job client must contain the completed job.
    job = fake_job_client.get_job(job_id)
    assert job["status"] == "completed"
    assert job["progress"] == 100
    assert job["result"]["job_id"] == job_id


def test_run_pipeline_job_failure_branch(monkeypatch: pytest.MonkeyPatch, fake_job_client) -> None:
    """When the orchestrator raises, the background runner marks the job failed."""
    monkeypatch.setattr(job_runner, "job_manager", fake_job_client)
    fake_job_client.create_job("j-fail", status="pending")

    class _RaisingOrch:
        def __init__(self, **_kw):
            pass

        def run(self, request, job_id, update_cb=None):
            raise RuntimeError("boom")

    monkeypatch.setattr(job_runner, "SalesPodOrchestrator", _RaisingOrch)

    # Build a minimal request — pass-through values fine since orchestrator raises.
    request = api_main.SalesPipelineRequest(
        product_name="P",
        value_proposition="A valid value proposition.",
        icp={"industry": ["SaaS"]},
    )
    api_main._run_pipeline_job("j-fail", request)
    job = fake_job_client.get_job("j-fail")
    assert job["status"] == "failed"
    assert job["error"] == "boom"
    assert job["current_stage"] == "failed"


def _minimal_request() -> "api_main.SalesPipelineRequest":
    return api_main.SalesPipelineRequest(
        product_name="P",
        value_proposition="A valid value proposition.",
        icp={"industry": ["SaaS"]},
    )


def test_run_pipeline_job_skips_when_already_cancelled(
    monkeypatch: pytest.MonkeyPatch, fake_job_client
) -> None:
    """A job that reached a terminal state before the runner starts (e.g. a
    Temporal workflow that sat queued while the user cancelled the job) must
    NOT be resurrected — the orchestrator is never constructed and the
    terminal status is preserved."""
    monkeypatch.setattr(job_runner, "job_manager", fake_job_client)
    fake_job_client.create_job("j-cancelled", status="cancelled")

    class _NeverRunOrch:
        def __init__(self, **_kw):  # pragma: no cover - must not be constructed
            raise AssertionError("orchestrator must not run for a terminal job")

    monkeypatch.setattr(job_runner, "SalesPodOrchestrator", _NeverRunOrch)

    api_main._run_pipeline_job("j-cancelled", _minimal_request())

    job = fake_job_client.get_job("j-cancelled")
    assert job["status"] == "cancelled"  # untouched


def test_run_pipeline_job_does_not_clobber_cancel_landing_mid_run(
    monkeypatch: pytest.MonkeyPatch, fake_job_client
) -> None:
    """A cancel that lands while the orchestrator is running must not be
    overwritten by the COMPLETED write."""
    monkeypatch.setattr(job_runner, "job_manager", fake_job_client)
    fake_job_client.create_job("j-midcancel", status="pending")

    class _CancellingOrch:
        def __init__(self, **_kw):
            pass

        def run(self, request, job_id, update_cb=None):
            # Simulate a cancel landing while the pipeline runs.
            fake_job_client.update_job(job_id, status="cancelled")
            return SalesPipelineResult(
                job_id=job_id, entry_stage=request.entry_stage, product_name=request.product_name
            )

    monkeypatch.setattr(job_runner, "SalesPodOrchestrator", _CancellingOrch)

    api_main._run_pipeline_job("j-midcancel", _minimal_request())

    job = fake_job_client.get_job("j-midcancel")
    assert job["status"] == "cancelled"  # not overwritten with completed


# ---------------------------------------------------------------------------
# /sales/pipeline/status
# ---------------------------------------------------------------------------


def test_get_pipeline_status_404_when_missing(client: TestClient) -> None:
    resp = client.get("/sales/pipeline/status/no-such-job")
    assert resp.status_code == 404


def test_get_pipeline_status_returns_running_job(client: TestClient, fake_job_client) -> None:
    fake_job_client.create_job(
        "j-run",
        status="running",
        current_stage="prospecting",
        progress=20,
        product_name="ProductX",
        last_updated_at="2026-05-21T00:00:00+00:00",
    )
    resp = client.get("/sales/pipeline/status/j-run")
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == "j-run"
    assert body["status"] == "running"
    assert body["product_name"] == "ProductX"
    assert body["result"] is None


def test_get_pipeline_status_parses_result_when_present(
    client: TestClient, fake_job_client
) -> None:
    result = SalesPipelineResult(
        job_id="j-done", entry_stage=PipelineStage.PROSPECTING, product_name="P"
    ).model_dump()
    fake_job_client.create_job(
        "j-done",
        status="completed",
        current_stage="completed",
        progress=100,
        product_name="P",
        result=result,
    )
    resp = client.get("/sales/pipeline/status/j-done")
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["job_id"] == "j-done"


def test_get_pipeline_status_handles_unparseable_result(
    client: TestClient, fake_job_client
) -> None:
    """An obviously-bad result dict should make ``result`` come back as None."""
    fake_job_client.create_job(
        "j-bad",
        status="completed",
        current_stage="completed",
        progress=100,
        product_name="P",
        result={"random": "shape"},  # missing required fields
    )
    resp = client.get("/sales/pipeline/status/j-bad")
    assert resp.status_code == 200
    assert resp.json()["result"] is None


# ---------------------------------------------------------------------------
# /sales/pipeline/jobs (list)
# ---------------------------------------------------------------------------


def test_list_pipeline_jobs_returns_all_by_default(client: TestClient, fake_job_client) -> None:
    fake_job_client.create_job(
        "a", status="running", current_stage="x", progress=5, product_name="A"
    )
    fake_job_client.create_job(
        "b", status="completed", current_stage="completed", progress=100, product_name="B"
    )
    resp = client.get("/sales/pipeline/jobs")
    assert resp.status_code == 200
    ids = {item["job_id"] for item in resp.json()}
    assert ids == {"a", "b"}


def test_list_pipeline_jobs_running_only_filter(client: TestClient, fake_job_client) -> None:
    fake_job_client.create_job("a", status="running", product_name="A")
    fake_job_client.create_job("b", status="completed", product_name="B")
    resp = client.get("/sales/pipeline/jobs?running_only=true")
    assert resp.status_code == 200
    ids = {item["job_id"] for item in resp.json()}
    assert ids == {"a"}


# ---------------------------------------------------------------------------
# /sales/pipeline/job/{id}/cancel
# ---------------------------------------------------------------------------


def test_cancel_pipeline_job_marks_cancelled(client: TestClient, fake_job_client) -> None:
    fake_job_client.create_job("j", status="running", product_name="P")
    resp = client.post("/sales/pipeline/job/j/cancel")
    assert resp.status_code == 200
    assert fake_job_client.get_job("j")["status"] == "cancelled"


def test_cancel_pipeline_job_404_when_missing(client: TestClient) -> None:
    assert client.post("/sales/pipeline/job/missing/cancel").status_code == 404


def test_cancel_pipeline_job_400_when_terminal(client: TestClient, fake_job_client) -> None:
    fake_job_client.create_job("j", status="completed", product_name="P")
    resp = client.post("/sales/pipeline/job/j/cancel")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /sales/pipeline/job/{id}
# ---------------------------------------------------------------------------


def test_delete_pipeline_job_404_when_missing(client: TestClient) -> None:
    assert client.delete("/sales/pipeline/job/missing").status_code == 404


def test_delete_pipeline_job_409_when_active(client: TestClient, fake_job_client) -> None:
    fake_job_client.create_job("j", status="running", product_name="P")
    resp = client.delete("/sales/pipeline/job/j")
    assert resp.status_code == 409


def test_delete_pipeline_job_succeeds_when_terminal(client: TestClient, fake_job_client) -> None:
    fake_job_client.create_job("j", status="completed", product_name="P")
    resp = client.delete("/sales/pipeline/job/j")
    assert resp.status_code == 200
    assert fake_job_client.get_job("j") is None


def test_delete_pipeline_job_404_when_underlying_delete_returns_false(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the job is found but delete_job returns False, surface 404."""

    class _StubJM:
        def get_job(self, job_id):
            return {"status": "completed", "product_name": "P"}

        def delete_job(self, job_id):
            return False

    monkeypatch.setattr(api_main, "_job_manager", _StubJM())
    resp = client.delete("/sales/pipeline/job/j")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /sales/prospect
# ---------------------------------------------------------------------------


def test_prospect_returns_orchestrator_output(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sample_icp_payload, sample_prospect
) -> None:
    class _Orch:
        def prospect_only(self, **kw):
            return [sample_prospect]

    monkeypatch.setattr(api_main, "SalesPodOrchestrator", _Orch)
    resp = client.post(
        "/sales/prospect",
        json={
            "icp": sample_icp_payload,
            "product_name": "P",
            "value_proposition": "value",
            "max_prospects": 3,
            "company_context": "",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["prospects"][0]["company_name"] == "Acme Corp"


# ---------------------------------------------------------------------------
# /sales/outreach
# ---------------------------------------------------------------------------


def test_generate_outreach_returns_sequences_and_skipped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sample_prospect
) -> None:
    p_kept = sample_prospect
    p_skipped = Prospect(id="prs_missing", company_name="Beta")
    dossier = ProspectDossier(
        prospect_id=p_kept.id,
        full_name="Jane",
        current_title="VP",
        current_company="Acme",
    )
    sequence = OutreachSequence(
        prospect=p_kept,
        dossier_id="d1",
        dossier_confidence=0.8,
        variants=[OutreachVariant(angle="company_soft_opener", personalization_grade="fallback")],
    )

    class _Orch:
        def load_dossiers_for_prospects(self, prospects):
            return {p_kept.id: dossier}

        def outreach_only(self, **kw):
            return [sequence]

    monkeypatch.setattr(api_main, "SalesPodOrchestrator", _Orch)
    resp = client.post(
        "/sales/outreach",
        json={
            "prospects": [p_kept.model_dump(), p_skipped.model_dump()],
            "product_name": "P",
            "value_proposition": "value",
            "case_study_snippets": [],
            "company_context": "",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["skipped_prospect_ids"] == [p_skipped.id]


# ---------------------------------------------------------------------------
# /sales/qualify
# ---------------------------------------------------------------------------


def test_qualify_lead_500_when_orchestrator_returns_none(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sample_prospect
) -> None:
    class _Orch:
        def qualify_only(self, **kw):
            return None

    monkeypatch.setattr(api_main, "SalesPodOrchestrator", _Orch)
    resp = client.post(
        "/sales/qualify",
        json={
            "prospect": sample_prospect.model_dump(),
            "product_name": "P",
            "value_proposition": "V",
            "call_notes": "",
        },
    )
    assert resp.status_code == 500


def test_qualify_lead_returns_score(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sample_prospect
) -> None:
    score = QualificationScore(
        prospect=sample_prospect,
        bant=BANTScore(budget=8, authority=9, need=7, timeline=8),
        meddic=MEDDICScore(
            metrics_identified=True,
            economic_buyer_known=True,
            decision_criteria_understood=True,
            decision_process_mapped=True,
            identify_pain=True,
            champion_found=True,
        ),
        overall_score=0.85,
        value_creation_level=3,
        recommended_action="advance",
    )

    class _Orch:
        def qualify_only(self, **kw):
            return score

    monkeypatch.setattr(api_main, "SalesPodOrchestrator", _Orch)
    resp = client.post(
        "/sales/qualify",
        json={
            "prospect": sample_prospect.model_dump(),
            "product_name": "P",
            "value_proposition": "V",
            "call_notes": "",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["overall_score"] == 0.85


# ---------------------------------------------------------------------------
# /sales/nurture
# ---------------------------------------------------------------------------


def test_build_nurture_returns_sequences(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sample_prospect
) -> None:
    nurture = NurtureSequence(prospect=sample_prospect, duration_days=30)

    class _Orch:
        def nurture_only(self, **kw):
            return [nurture]

    monkeypatch.setattr(api_main, "SalesPodOrchestrator", _Orch)
    resp = client.post(
        "/sales/nurture",
        json={
            "prospects": [sample_prospect.model_dump()],
            "product_name": "P",
            "value_proposition": "V",
            "duration_days": 30,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 1


# ---------------------------------------------------------------------------
# /sales/proposal
# ---------------------------------------------------------------------------


def test_write_proposal_500_when_orchestrator_returns_none(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sample_prospect
) -> None:
    class _Orch:
        def propose_only(self, req):
            return None

    monkeypatch.setattr(api_main, "SalesPodOrchestrator", _Orch)
    resp = client.post(
        "/sales/proposal",
        json={
            "prospect": sample_prospect.model_dump(),
            "product_name": "P",
            "value_proposition": "V",
            "annual_cost_usd": 25000.0,
            "discovery_notes": "",
            "case_study_snippets": [],
            "company_context": "",
        },
    )
    assert resp.status_code == 500


def test_write_proposal_returns_serialized_proposal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sample_prospect
) -> None:
    proposal = SalesProposal(
        prospect=sample_prospect,
        executive_summary="...",
        situation_analysis="...",
        proposed_solution="...",
        roi_model=ROIModel(
            annual_cost_usd=25000.0,
            estimated_annual_benefit_usd=70000.0,
            payback_months=6.0,
            roi_percentage=180.0,
        ),
    )

    class _Orch:
        def propose_only(self, req):
            return proposal

    monkeypatch.setattr(api_main, "SalesPodOrchestrator", _Orch)
    resp = client.post(
        "/sales/proposal",
        json={
            "prospect": sample_prospect.model_dump(),
            "product_name": "P",
            "value_proposition": "V",
            "annual_cost_usd": 25000.0,
            "discovery_notes": "",
            "case_study_snippets": [],
            "company_context": "",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["roi_model"]["payback_months"] == 6.0


# ---------------------------------------------------------------------------
# /sales/coaching
# ---------------------------------------------------------------------------


def test_get_coaching_500_when_orchestrator_returns_none(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sample_prospect
) -> None:
    class _Orch:
        def coach_only(self, **kw):
            return None

    monkeypatch.setattr(api_main, "SalesPodOrchestrator", _Orch)
    resp = client.post(
        "/sales/coaching",
        json={
            "prospects": [sample_prospect.model_dump()],
            "product_name": "P",
            "pipeline_context": "",
        },
    )
    assert resp.status_code == 500


def test_get_coaching_returns_report(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sample_prospect
) -> None:
    report = PipelineCoachingReport(prospects_reviewed=1, coaching_summary="OK")

    class _Orch:
        def coach_only(self, **kw):
            return report

    monkeypatch.setattr(api_main, "SalesPodOrchestrator", _Orch)
    resp = client.post(
        "/sales/coaching",
        json={
            "prospects": [sample_prospect.model_dump()],
            "product_name": "P",
            "pipeline_context": "",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["coaching_summary"] == "OK"


# ---------------------------------------------------------------------------
# /sales/prospect/deep-research
# ---------------------------------------------------------------------------


def test_deep_research_endpoint_returns_result(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sample_icp_payload
) -> None:
    deep_result = DeepResearchResult(
        list_id="plst_x",
        product_name="P",
        generated_at="2026-05-21T00:00:00+00:00",
        total_prospects=0,
        companies_represented=0,
        entries=[],
    )

    class _Orch:
        def deep_research_only(self, body, dossier_url_builder=None):
            # Exercise the url-builder branch by calling it.
            assert dossier_url_builder("dsr_test").endswith("dsr_test")
            return deep_result

    monkeypatch.setattr(api_main, "SalesPodOrchestrator", _Orch)
    resp = client.post(
        "/sales/prospect/deep-research",
        json={
            "product_name": "P",
            "value_proposition": "Value with at least ten chars",
            "icp": sample_icp_payload,
            "target_prospects": 10,
            "max_per_company": 2,
            "company_context": "",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["list_id"] == "plst_x"


# ---------------------------------------------------------------------------
# /sales/dossiers/{id}
# ---------------------------------------------------------------------------


def test_get_dossier_503_when_store_import_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the dossier_store module raises at import time, surface 503."""
    import builtins

    real_import = builtins.__import__

    def _import(name, *args, **kwargs):
        if name == "sales_team.dossier_store":
            raise ImportError("missing psycopg")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _import)
    resp = client.get("/sales/dossiers/dsr_x")
    assert resp.status_code == 503


def test_get_dossier_503_when_store_construct_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sales_team import dossier_store as ds_mod

    class _BrokenStore:
        def __init__(self):
            raise RuntimeError("pool init failed")

    monkeypatch.setattr(ds_mod, "DossierStore", _BrokenStore)
    resp = client.get("/sales/dossiers/dsr_x")
    assert resp.status_code == 503


def test_get_dossier_404_when_store_returns_none(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sales_team import dossier_store as ds_mod

    class _EmptyStore:
        def get_dossier(self, dossier_id):
            return None

    monkeypatch.setattr(ds_mod, "DossierStore", _EmptyStore)
    resp = client.get("/sales/dossiers/dsr_missing")
    assert resp.status_code == 404


def test_get_dossier_503_when_store_call_raises(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sales_team import dossier_store as ds_mod

    class _BrokenStore:
        def get_dossier(self, dossier_id):
            raise RuntimeError("Postgres down")

    monkeypatch.setattr(ds_mod, "DossierStore", _BrokenStore)
    resp = client.get("/sales/dossiers/dsr_x")
    assert resp.status_code == 503


def test_get_dossier_returns_payload(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from sales_team import dossier_store as ds_mod

    dossier = ProspectDossier(
        prospect_id="prs_z",
        full_name="Jane",
        current_title="VP",
        current_company="Acme",
    )

    class _Store:
        def get_dossier(self, dossier_id):
            return dossier

    monkeypatch.setattr(ds_mod, "DossierStore", _Store)
    resp = client.get("/sales/dossiers/dsr_x")
    assert resp.status_code == 200
    assert resp.json()["full_name"] == "Jane"


# ---------------------------------------------------------------------------
# /sales/prospect-lists/{id} + list
# ---------------------------------------------------------------------------


def test_get_prospect_list_404_when_store_returns_none(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sales_team import dossier_store as ds_mod

    class _Store:
        def get_prospect_list(self, list_id):
            return None

    monkeypatch.setattr(ds_mod, "DossierStore", _Store)
    resp = client.get("/sales/prospect-lists/plst_missing")
    assert resp.status_code == 404


def test_get_prospect_list_503_when_store_call_raises(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sales_team import dossier_store as ds_mod

    class _Store:
        def get_prospect_list(self, list_id):
            raise RuntimeError("down")

    monkeypatch.setattr(ds_mod, "DossierStore", _Store)
    assert client.get("/sales/prospect-lists/plst_x").status_code == 503


def test_get_prospect_list_returns_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sales_team import dossier_store as ds_mod

    res = DeepResearchResult(
        list_id="plst_x",
        product_name="P",
        generated_at="2026-05-21T00:00:00+00:00",
        total_prospects=0,
        companies_represented=0,
    )

    class _Store:
        def get_prospect_list(self, list_id):
            return res

    monkeypatch.setattr(ds_mod, "DossierStore", _Store)
    resp = client.get("/sales/prospect-lists/plst_x")
    assert resp.status_code == 200
    assert resp.json()["list_id"] == "plst_x"


def test_list_prospect_lists_returns_summaries(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sales_team import dossier_store as ds_mod

    class _Store:
        def list_prospect_lists(self, limit):
            return [
                {
                    "list_id": "plst_a",
                    "product_name": "A",
                    "total_prospects": 1,
                    "companies_represented": 1,
                    "generated_at": "now",
                }
            ]

    monkeypatch.setattr(ds_mod, "DossierStore", _Store)
    resp = client.get("/sales/prospect-lists?limit=10")
    assert resp.status_code == 200
    assert resp.json()[0]["product_name"] == "A"


def test_list_prospect_lists_503_on_store_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sales_team import dossier_store as ds_mod

    class _Store:
        def list_prospect_lists(self, limit):
            raise RuntimeError("Postgres down")

    monkeypatch.setattr(ds_mod, "DossierStore", _Store)
    assert client.get("/sales/prospect-lists").status_code == 503


def test_list_prospect_lists_caps_limit_to_reasonable_range(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Limit must be clamped to [1, 200] before hitting the store."""
    from sales_team import dossier_store as ds_mod

    captured: list[int] = []

    class _Store:
        def list_prospect_lists(self, limit):
            captured.append(limit)
            return []

    monkeypatch.setattr(ds_mod, "DossierStore", _Store)
    client.get("/sales/prospect-lists?limit=0")  # below the floor
    client.get("/sales/prospect-lists?limit=99999")  # above the ceiling
    assert captured == [1, 200]


# ---------------------------------------------------------------------------
# /sales/outcomes/{stage,deal}
# ---------------------------------------------------------------------------


def test_record_stage_outcome_endpoint(client: TestClient) -> None:
    resp = client.post(
        "/sales/outcomes/stage",
        json={
            "company_name": "Acme",
            "stage": "outreach",
            "outcome": "converted",
            "industry": "SaaS",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome_id"]
    assert "Acme" in body["message"]


def test_record_deal_outcome_endpoint(client: TestClient) -> None:
    resp = client.post(
        "/sales/outcomes/deal",
        json={
            "company_name": "Acme",
            "result": "won",
            "final_stage_reached": "closed_won",
            "deal_size_usd": 100000.0,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome_id"]
    assert "won" in body["message"]


def test_get_outcome_summary_returns_counts(client: TestClient) -> None:
    resp = client.get("/sales/outcomes/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"stage_outcomes", "deal_outcomes", "has_insights"}


def test_list_stage_outcomes_returns_recorded_entries(client: TestClient) -> None:
    client.post(
        "/sales/outcomes/stage",
        json={"company_name": "X", "stage": "outreach", "outcome": "converted"},
    )
    resp = client.get("/sales/outcomes/stage?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["company_name"] == "X"


def test_list_deal_outcomes_returns_recorded_entries(client: TestClient) -> None:
    client.post(
        "/sales/outcomes/deal",
        json={
            "company_name": "X",
            "result": "won",
            "final_stage_reached": "closed_won",
        },
    )
    resp = client.get("/sales/outcomes/deal?limit=10")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_list_stage_outcomes_caps_limit_at_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[int] = []

    def _fake_load(limit):
        captured.append(limit)
        return []

    monkeypatch.setattr(api_main, "load_stage_outcomes", _fake_load)
    client.get("/sales/outcomes/stage?limit=9999")
    assert captured == [500]


def test_list_deal_outcomes_caps_limit_at_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[int] = []

    def _fake_load(limit):
        captured.append(limit)
        return []

    monkeypatch.setattr(api_main, "load_deal_outcomes", _fake_load)
    client.get("/sales/outcomes/deal?limit=9999")
    assert captured == [500]


# ---------------------------------------------------------------------------
# /sales/insights + /sales/insights/refresh
# ---------------------------------------------------------------------------


def test_get_insights_404_when_no_history(client: TestClient) -> None:
    resp = client.get("/sales/insights")
    assert resp.status_code == 404


def test_get_insights_returns_persisted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    insights = LearningInsights(
        total_outcomes_analyzed=2,
        win_rate=0.5,
        winning_patterns=["mt"],
        insights_version=1,
        generated_at="2026-05-21T00:00:00+00:00",
    )
    monkeypatch.setattr(api_main, "load_current_insights", lambda: insights)
    resp = client.get("/sales/insights")
    assert resp.status_code == 200
    assert resp.json()["win_rate"] == 0.5


def test_refresh_insights_runs_engine(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    refreshed = LearningInsights(
        total_outcomes_analyzed=3,
        win_rate=0.66,
        insights_version=2,
        generated_at="2026-05-21T00:00:00+00:00",
    )

    class _Engine:
        def refresh(self):
            return refreshed

    monkeypatch.setattr(api_main, "LearningEngine", _Engine)
    resp = client.post("/sales/insights/refresh")
    assert resp.status_code == 200
    body = resp.json()
    assert body["insights_version"] == 2
    assert body["total_outcomes_analyzed"] == 3
    assert body["win_rate"] == 0.66


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health_returns_ok_with_counts(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "stage_outcomes_recorded" in body
    assert "insights_available" in body


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


def test_lifespan_swallows_postgres_import_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lifespan must not crash app boot when shared_postgres is unavailable."""
    import builtins

    real_import = builtins.__import__

    def _import(name, *args, **kwargs):
        # Block both possible imports inside the lifespan branches.
        if name in ("sales_team.postgres", "shared_postgres"):
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _import)
    # The TestClient context manager triggers startup + shutdown.
    with TestClient(api_main.app) as c:
        assert c.get("/health").status_code == 200


def test_lifespan_runs_register_and_close_when_imports_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When shared_postgres imports succeed, ``register_team_schemas`` and
    ``close_pool`` are both called (exercising lines 65-67 and 74)."""
    import shared_postgres as sp_mod

    captured: dict[str, int] = {"register": 0, "close": 0}

    def _fake_register(*schemas):
        captured["register"] += 1

    def _fake_close():
        captured["close"] += 1

    monkeypatch.setattr(sp_mod, "register_team_schemas", _fake_register)
    monkeypatch.setattr(sp_mod, "close_pool", _fake_close)
    with TestClient(api_main.app):
        pass
    assert captured["register"] == 1
    assert captured["close"] == 1


def test_lifespan_swallows_close_pool_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """When close_pool raises during shutdown, the warning is logged but the
    lifespan exit succeeds (covers the except branch on lines 75-76)."""
    import shared_postgres as sp_mod

    monkeypatch.setattr(sp_mod, "register_team_schemas", lambda *a, **kw: None)
    monkeypatch.setattr(sp_mod, "close_pool", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    with TestClient(api_main.app):
        pass  # exiting triggers close_pool


def test_deep_research_url_builder_fallback_when_url_for_raises(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sample_icp_payload
) -> None:
    """If request.url_for raises (e.g. routing context missing), the URL
    builder must fall back to the hard-coded /api/sales/dossiers/<id>
    shape."""
    deep_result = DeepResearchResult(
        list_id="plst_x",
        product_name="P",
        generated_at="2026-05-21T00:00:00+00:00",
        total_prospects=0,
        companies_represented=0,
        entries=[],
    )

    class _Orch:
        def deep_research_only(self, body, dossier_url_builder=None):
            # Trigger the fallback by accessing the builder after patching
            # url_for to raise. The builder will catch and emit the fallback.
            url = dossier_url_builder("dsr_abc")
            assert url == "/api/sales/dossiers/dsr_abc"
            return deep_result

    # Patch the FastAPI Request.url_for so the inner builder's try/except
    # routes to the fallback.
    from starlette.requests import Request as StarletteRequest

    def _bad_url_for(self, *args, **kwargs):
        raise RuntimeError("no routing context")

    monkeypatch.setattr(StarletteRequest, "url_for", _bad_url_for, raising=True)
    monkeypatch.setattr(api_main, "SalesPodOrchestrator", _Orch)
    resp = client.post(
        "/sales/prospect/deep-research",
        json={
            "product_name": "P",
            "value_proposition": "Value with at least ten chars",
            "icp": sample_icp_payload,
            "target_prospects": 10,
            "max_per_company": 2,
            "company_context": "",
        },
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Extra: SalesPipelineResult round-trip through DiscoveryPlan / ClosingStrategy
# (sanity that route models import cleanly under coverage)
# ---------------------------------------------------------------------------


def test_route_models_import_cleanly() -> None:
    DiscoveryPlan(
        prospect=Prospect(company_name="x"),
        spin_questions=SPINQuestions(),
    )
    ClosingStrategy(
        prospect=Prospect(company_name="x"),
        recommended_close_technique=CloseType.SUMMARY,
        close_script="s",
    )
    # Touch the enum-value branches without provoking ruff for unused locals.
    assert DealResult.WON.value == "won"
    assert OutcomeResult.CONVERTED.value == "converted"
