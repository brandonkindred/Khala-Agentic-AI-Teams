"""Additional tests for ``sales_team.learning_engine``.

Existing tests in ``test_sales_team.py`` cover the empty-outcomes and
happy-path branches of ``LearningEngine.refresh``. This module covers the
``format_insights_for_prompt`` helper and the remaining branches in
``refresh``:

  * version increments off an existing snapshot,
  * defensive total_outcomes_analyzed backfill when the LLM under-reports,
  * default-load of stage/deal outcomes when caller passes ``None``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from llm_service.interface import LLMClient
from sales_team import learning_engine as le_mod
from sales_team.learning_engine import (
    LearningEngine,
    format_insights_for_prompt,
)
from sales_team.models import (
    DealOutcome,
    DealResult,
    LearningInsights,
    OutcomeResult,
    PipelineStage,
    StageOutcome,
)

# ---------------------------------------------------------------------------
# Canned LLM client (local to keep this file self-contained)
# ---------------------------------------------------------------------------


class CannedLLM(LLMClient):
    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt})
        if not self._responses:
            raise AssertionError("CannedLLM out of responses")
        return self._responses.pop(0)


# ---------------------------------------------------------------------------
# format_insights_for_prompt
# ---------------------------------------------------------------------------


def test_format_returns_empty_when_insights_none() -> None:
    assert format_insights_for_prompt(None) == ""


def test_format_returns_empty_when_no_outcomes_analyzed() -> None:
    assert format_insights_for_prompt(LearningInsights(total_outcomes_analyzed=0)) == ""


def test_format_renders_all_optional_sections() -> None:
    insights = LearningInsights(
        total_outcomes_analyzed=12,
        win_rate=0.42,
        insights_version=3,
        winning_patterns=["multi-thread", "EB on call"],
        losing_patterns=["single-thread", "no champion"],
        top_performing_industries=["SaaS", "FinTech"],
        common_objections=["price", "timing"],
        best_close_techniques=["summary", "alternative_choice"],
        best_outreach_angles=["trigger_event", "peer_proof"],
        actionable_recommendations=["multi-thread earlier", "ask for EB"],
        avg_sales_cycle_days=42.0,
        stage_conversion_rates={"qualification": 0.4, "discovery": 0.7},
    )
    out = format_insights_for_prompt(insights)
    assert "Learned from 12 past outcomes" in out
    assert "What's working" in out
    assert "multi-thread" in out
    assert "Watch out for" in out
    assert "Top industries" in out and "SaaS" in out
    assert "Most frequent objections" in out
    assert "Best close techniques" in out
    assert "High-reply outreach angles" in out
    assert "Top recommendations" in out
    assert "Avg sales cycle (won)" in out
    assert "Biggest funnel leak" in out
    # Worst converter is "qualification" with 0.4 conversion.
    assert "qualification" in out


def test_format_omits_optional_sections_when_lists_empty() -> None:
    """Minimal insights with only total_outcomes_analyzed=1 → only header."""
    insights = LearningInsights(total_outcomes_analyzed=1)
    out = format_insights_for_prompt(insights)
    assert "Learned from 1 past outcomes" in out
    assert "What's working" not in out
    assert "Watch out for" not in out
    assert "Top industries" not in out
    assert "Biggest funnel leak" not in out


# ---------------------------------------------------------------------------
# LearningEngine.refresh: version increments and defensive backfill
# ---------------------------------------------------------------------------


def test_refresh_increments_version_off_existing_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = LearningInsights(insights_version=5, total_outcomes_analyzed=10)
    saved: Dict[str, Any] = {}

    monkeypatch.setattr(le_mod, "load_current_insights", lambda: existing)
    monkeypatch.setattr(le_mod, "save_insights", lambda i: saved.setdefault("i", i))

    llm = CannedLLM(
        [
            {
                "total_outcomes_analyzed": 1,
                "win_rate": 0.0,
                "winning_patterns": [],
                "actionable_recommendations": [],
            }
        ]
    )
    engine = LearningEngine(llm_client=llm)
    stage = StageOutcome(
        company_name="A",
        stage=PipelineStage.OUTREACH,
        outcome=OutcomeResult.CONVERTED,
    )
    insights = engine.refresh(stage_outcomes=[stage], deal_outcomes=[])
    assert insights.insights_version == 6  # existing 5 + 1
    assert saved["i"] is insights


def test_refresh_backfills_total_outcomes_when_llm_under_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the LLM forgets to set total_outcomes_analyzed, the engine must
    fill it with the actual record count so the UI is honest."""
    monkeypatch.setattr(le_mod, "load_current_insights", lambda: None)
    monkeypatch.setattr(le_mod, "save_insights", lambda i: None)

    llm = CannedLLM(
        [
            {
                "total_outcomes_analyzed": 0,  # LLM forgot
                "win_rate": 0.5,
                "winning_patterns": [],
                "actionable_recommendations": [],
            }
        ]
    )
    engine = LearningEngine(llm_client=llm)
    stages = [
        StageOutcome(
            company_name=f"A{i}",
            stage=PipelineStage.OUTREACH,
            outcome=OutcomeResult.CONVERTED,
        )
        for i in range(3)
    ]
    deals = [
        DealOutcome(
            company_name="A0",
            final_stage_reached=PipelineStage.CLOSED_WON,
            result=DealResult.WON,
        )
    ]
    insights = engine.refresh(stage_outcomes=stages, deal_outcomes=deals)
    # 3 stages + 1 deal = 4
    assert insights.total_outcomes_analyzed == 4


def test_refresh_loads_outcomes_when_caller_passes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``stage_outcomes`` / ``deal_outcomes`` are None, the engine must
    fall back to ``load_stage_outcomes()`` / ``load_deal_outcomes()``."""
    captured: dict[str, int] = {"stage_loaded": 0, "deal_loaded": 0}

    def _fake_load_stage():
        captured["stage_loaded"] += 1
        return []

    def _fake_load_deal():
        captured["deal_loaded"] += 1
        return []

    monkeypatch.setattr(le_mod, "load_stage_outcomes", _fake_load_stage)
    monkeypatch.setattr(le_mod, "load_deal_outcomes", _fake_load_deal)
    monkeypatch.setattr(le_mod, "load_current_insights", lambda: None)
    monkeypatch.setattr(le_mod, "save_insights", lambda i: None)

    llm = CannedLLM([])  # zero outcomes, never called
    engine = LearningEngine(llm_client=llm)
    insights = engine.refresh()  # default args
    assert insights.total_outcomes_analyzed == 0
    assert captured == {"stage_loaded": 1, "deal_loaded": 1}
