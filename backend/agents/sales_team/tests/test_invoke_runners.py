"""Tests for Agent Console invoke-shim entrypoints in invoke_runners.py."""

from __future__ import annotations

from typing import Any, Optional

import pytest

from sales_team import invoke_runners
from sales_team.models import CloseType, ClosingStrategyBody, ProspectDossier


@pytest.fixture()
def closer_request_body() -> dict[str, Any]:
    return {
        "prospect": {
            "id": "prs_testprospect",
            "company_name": "Acme Corp",
            "contact_name": "Jane Smith",
            "contact_title": "VP of Sales",
        },
        "proposal": {
            "roi_model": {
                "annual_cost_usd": 25000.0,
                "estimated_annual_benefit_usd": 70000.0,
                "payback_months": 6.0,
                "roi_percentage": 180.0,
            },
        },
        "product_name": "ProductX",
        "value_proposition": "vp",
    }


@pytest.fixture()
def sample_dossier() -> ProspectDossier:
    return ProspectDossier(
        dossier_id="dsr_testdossier1",
        prospect_id="prs_testprospect",
        full_name="Jane Smith",
        current_title="VP of Sales",
        current_company="Acme Corp",
        executive_summary="Jane runs sales at Acme Corp, a Series-B SaaS company.",
        trigger_events=["Acme Corp announced Series B funding ($40M)"],
        confidence=0.82,
    )


class _StubCloserAgent:
    """Records the dossier passed to develop_strategy instead of calling an LLM."""

    last_dossier: Optional[ProspectDossier] = None

    def develop_strategy(self, **kwargs: Any) -> ClosingStrategyBody:
        type(self).last_dossier = kwargs.get("dossier")
        return ClosingStrategyBody(
            recommended_close_technique=CloseType.SUMMARY,
            close_script="Shall we sign?",
            urgency_framing="Q-end",
            walk_away_criteria="no budget",
            emotional_intelligence_notes="analytical",
        )


def test_invoke_closer_forwards_dossier_to_agent(
    monkeypatch: pytest.MonkeyPatch,
    closer_request_body: dict[str, Any],
    sample_dossier: ProspectDossier,
) -> None:
    monkeypatch.setattr(invoke_runners, "CloserAgent", _StubCloserAgent)
    body = {**closer_request_body, "dossier": sample_dossier.model_dump(mode="json")}

    result = invoke_runners.invoke_closer(body)

    assert result["recommended_close_technique"] == "summary"
    assert _StubCloserAgent.last_dossier is not None
    assert _StubCloserAgent.last_dossier.dossier_id == sample_dossier.dossier_id


def test_invoke_closer_without_dossier_defaults_to_none(
    monkeypatch: pytest.MonkeyPatch, closer_request_body: dict[str, Any]
) -> None:
    monkeypatch.setattr(invoke_runners, "CloserAgent", _StubCloserAgent)

    invoke_runners.invoke_closer(closer_request_body)

    assert _StubCloserAgent.last_dossier is None
