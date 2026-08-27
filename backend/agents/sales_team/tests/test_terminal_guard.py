"""Cross-pipeline regression sweep for the shared terminal-status guard contract.

Closes out epic #7247: proves the sales pipeline and deep-research pipeline's
prepare/finalize activities produce IDENTICAL categories of externally
observable behavior for every job status, because they all route through the
single ``_terminal_guard`` implementation in
``sales_team.temporal.activities`` rather than any hand-rolled duplicate.
"""

from __future__ import annotations

import pytest
from temporalio.exceptions import ApplicationError

from sales_team import job_runner
from sales_team.models import DeepResearchRequest, SalesPipelineRequest
from sales_team.temporal import activities as acts
from sales_team.temporal import deep_research_activities as dra
from sales_team.temporal.phase_models import DeepResearchContext, SalesRunContext

_SALES_REQUEST = {
    "product_name": "ProductX",
    "value_proposition": "Save 20% on outbound time",
    "icp": {"industry": ["SaaS"]},
}
_DEEP_RESEARCH_REQUEST = {
    "product_name": "ProductX",
    "value_proposition": "Save 20% on outbound time",
    "icp": {"industry": ["SaaS"]},
    "target_prospects": 10,
    "max_per_company": 2,
}

# (status, category) — category is the externally-observable behavior class
# every prepare/finalize call site must fall into for that status.
_STATUS_CASES = [
    ("missing", "raise_missing"),
    ("running", "proceed"),
    ("failed", "raise_failed"),
    ("cancelled", "stop"),
    ("interrupted", "stop"),
    ("completed", "stop"),
]


def _sales_ctx(job_id: str) -> dict:
    return SalesRunContext(
        request=SalesPipelineRequest(**_SALES_REQUEST), job_id=job_id
    ).model_dump(mode="json")


def _sales_result(job_id: str) -> dict:
    return {
        "job_id": job_id,
        "entry_stage": "prospecting",
        "product_name": "ProductX",
        "prospects": [],
    }


def _deep_research_ctx(job_id: str) -> dict:
    req = DeepResearchRequest(**_DEEP_RESEARCH_REQUEST)
    return DeepResearchContext(
        request=req, job_id=job_id, icp_json=req.icp.model_dump_json()
    ).model_dump(mode="json")


def _seed(fake_job_client, job_id: str, status: str) -> None:
    if status != "missing":
        fake_job_client.create_job(job_id, status=status)


@pytest.fixture(autouse=True)
def _fake_jobs(monkeypatch: pytest.MonkeyPatch, fake_job_client) -> None:
    monkeypatch.setattr(job_runner, "job_manager", fake_job_client)


@pytest.fixture(autouse=True)
def _stub_orchestrator_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the non-guard work so the PROCEED path exercises only guard
    routing, not real orchestrator/insights/persistence logic."""
    from sales_team.models import DeepResearchResult

    monkeypatch.setattr("sales_team.outcome_store.load_current_insights", lambda: None)
    monkeypatch.setattr(
        "sales_team.orchestrator.record_prospecting_outcomes", lambda prospects, job_id: None
    )
    monkeypatch.setattr(
        "sales_team.orchestrator.assemble_and_persist_deep_research",
        lambda **kwargs: DeepResearchResult(
            product_name="ProductX", total_prospects=0, companies_represented=0
        ),
    )


@pytest.mark.parametrize("status,category", _STATUS_CASES)
def test_prepare_agrees_across_pipelines(fake_job_client, status, category):
    """prepare_sales_pipeline_activity and prepare_deep_research_activity must
    fall into the same behavior category for every job status."""
    _seed(fake_job_client, "job-1", status)
    _seed(fake_job_client, "job-2", status)

    def call_sales():
        return acts.prepare_sales_pipeline_activity("job-1", _SALES_REQUEST)

    def call_deep_research():
        return dra.prepare_deep_research_activity("job-2", _DEEP_RESEARCH_REQUEST)

    if category == "raise_missing":
        with pytest.raises(RuntimeError):
            call_sales()
        with pytest.raises(RuntimeError):
            call_deep_research()
    elif category == "raise_failed":
        with pytest.raises(ApplicationError) as sales_exc:
            call_sales()
        with pytest.raises(ApplicationError) as dr_exc:
            call_deep_research()
        assert sales_exc.value.non_retryable is True
        assert dr_exc.value.non_retryable is True
    elif category == "stop":
        assert call_sales()["stopped"] is True
        assert call_deep_research()["stopped"] is True
        assert fake_job_client.get_job("job-1")["status"] == status
        assert fake_job_client.get_job("job-2")["status"] == status
    else:  # proceed
        assert call_sales()["stopped"] is False
        assert call_deep_research()["stopped"] is False
        assert fake_job_client.get_job("job-1")["status"] == "running"
        assert fake_job_client.get_job("job-2")["status"] == "running"


@pytest.mark.parametrize("status,category", _STATUS_CASES)
def test_finalize_agrees_across_pipelines(fake_job_client, status, category):
    """finalize_sales_pipeline_activity and finalize_deep_research_activity
    must fall into the same behavior category for every job status."""
    _seed(fake_job_client, "job-1", status)
    _seed(fake_job_client, "job-2", status)

    def call_sales():
        return acts.finalize_sales_pipeline_activity(_sales_ctx("job-1"), _sales_result("job-1"))

    def call_deep_research():
        return dra.finalize_deep_research_activity(_deep_research_ctx("job-2"), [], [], [])

    if category == "raise_missing":
        with pytest.raises(RuntimeError):
            call_sales()
        with pytest.raises(RuntimeError):
            call_deep_research()
    elif category == "raise_failed":
        with pytest.raises(ApplicationError) as sales_exc:
            call_sales()
        with pytest.raises(ApplicationError) as dr_exc:
            call_deep_research()
        assert sales_exc.value.non_retryable is True
        assert dr_exc.value.non_retryable is True
    elif category == "stop":
        assert call_sales() == {"job_id": "job-1"}
        assert call_deep_research() == {"job_id": "job-2"}
        assert fake_job_client.get_job("job-1")["status"] == status
        assert fake_job_client.get_job("job-2")["status"] == status
    else:  # proceed
        assert call_sales() == {"job_id": "job-1"}
        assert call_deep_research() == {"job_id": "job-2"}
        assert fake_job_client.get_job("job-1")["status"] == "completed"
        assert fake_job_client.get_job("job-2")["status"] == "completed"
