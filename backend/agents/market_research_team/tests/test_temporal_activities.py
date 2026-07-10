"""Unit tests for the fine-grained market_research Temporal activities.

Each activity is exercised against the in-memory ``fake_job_client`` (autouse
via ``conftest``) and the mocked specialist agents, so no Temporal server or LLM
provider is needed. These pin the job-store status-ownership contract: RUNNING
by prepare, COMPLETED by finalize, FAILED by mark_failed only, and the
terminal-state guards that stop a cancelled run from doing work.
"""

from __future__ import annotations

import pytest
from temporalio.exceptions import ApplicationError

from market_research_team.models import RunMarketResearchRequest
from market_research_team.shared.job_store import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    create_job,
    get_job,
    update_job,
)
from market_research_team.temporal import activities as act
from market_research_team.temporal.phase_models import MarketResearchRunContext

_REQUEST = {
    "product_concept": "Interview analysis assistant",
    "target_users": "startup founders",
    "business_goal": "validate demand faster",
    "topology": "unified",
    "transcripts": ["Users want confidence before building features."],
    "human_approved": True,
}


def _ctx(job_id: str, *, topology: str = "unified", approved: bool = True) -> dict:
    req = RunMarketResearchRequest(
        product_concept="Concept X",
        target_users="Users",
        business_goal="Goal",
        topology=topology,
        human_approved=approved,
    )
    return MarketResearchRunContext(request=req, job_id=job_id).model_dump(mode="json")


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def test_prepare_writes_running_and_strips_transcripts() -> None:
    create_job("job-prep", request=_REQUEST)

    ctx = act.prepare_activity("job-prep", _REQUEST)

    assert ctx["stopped"] is False
    assert ctx["job_id"] == "job-prep"
    # Transcripts are stripped from the carried request (payload discipline).
    assert ctx["request"]["transcripts"] == []
    assert ctx["request"]["transcript_folder_path"] is None
    assert get_job("job-prep")["status"] == JOB_STATUS_RUNNING


def test_prepare_rejects_invalid_request_non_retryably() -> None:
    create_job("job-bad", request={})
    with pytest.raises(ApplicationError) as exc:
        act.prepare_activity("job-bad", {"product_concept": "x"})  # missing required fields
    assert exc.value.non_retryable is True


def test_prepare_raises_when_job_missing() -> None:
    with pytest.raises(RuntimeError, match="not found at prepare"):
        act.prepare_activity("job-ghost", _REQUEST)


def test_prepare_raises_non_retryably_when_already_failed() -> None:
    create_job("job-f", request=_REQUEST)
    update_job("job-f", status=JOB_STATUS_FAILED)
    with pytest.raises(ApplicationError) as exc:
        act.prepare_activity("job-f", _REQUEST)
    assert exc.value.non_retryable is True


def test_prepare_stops_on_clean_terminal_without_running_write() -> None:
    create_job("job-c", request=_REQUEST)
    update_job("job-c", status=JOB_STATUS_CANCELLED)

    ctx = act.prepare_activity("job-c", _REQUEST)

    assert ctx["stopped"] is True
    assert get_job("job-c")["status"] == JOB_STATUS_CANCELLED


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


def test_ingest_returns_source_text_pairs() -> None:
    create_job("job-i", request=_REQUEST)
    update_job("job-i", status=JOB_STATUS_RUNNING)

    loaded = act.ingest_activity("job-i", _REQUEST)

    assert loaded == [["inline_transcript_1", "Users want confidence before building features."]]


def test_ingest_returns_empty_when_job_terminal() -> None:
    create_job("job-i2", request=_REQUEST)
    update_job("job-i2", status=JOB_STATUS_CANCELLED)

    assert act.ingest_activity("job-i2", _REQUEST) == []


# ---------------------------------------------------------------------------
# per-stage LLM activities
# ---------------------------------------------------------------------------


def test_ux_one_returns_insight_dict() -> None:
    create_job("job-ux", request=_REQUEST)
    update_job("job-ux", status=JOB_STATUS_RUNNING)

    insight = act.ux_one_activity(_ctx("job-ux"), "src", "a transcript about pain")

    assert insight["source"] == "src"
    assert "user_jobs" in insight


def test_ux_one_raises_non_retryably_when_terminal() -> None:
    create_job("job-uxc", request=_REQUEST)
    update_job("job-uxc", status=JOB_STATUS_CANCELLED)

    with pytest.raises(ApplicationError) as exc:
        act.ux_one_activity(_ctx("job-uxc"), "src", "text")
    assert exc.value.non_retryable is True


def test_ux_one_reraises_generic_agent_failure(monkeypatch) -> None:
    """A non-ApplicationError agent failure is logged and re-raised for Temporal
    to retry (it is NOT swallowed)."""
    create_job("job-uxe", request=_REQUEST)
    update_job("job-uxe", status=JOB_STATUS_RUNNING)

    def _boom(self, source, transcript):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(
        "market_research_team.orchestrator.MarketResearchOrchestrator.ux_one", _boom
    )

    with pytest.raises(RuntimeError, match="agent exploded"):
        act.ux_one_activity(_ctx("job-uxe"), "src", "text")


def test_psychology_returns_signals_and_skips_when_terminal() -> None:
    create_job("job-p", request=_REQUEST)
    update_job("job-p", status=JOB_STATUS_RUNNING)
    insights = [
        {
            "source": "s",
            "user_jobs": ["j"],
            "pain_points": [],
            "desired_outcomes": [],
            "direct_quotes": [],
        }
    ]

    signals = act.psychology_activity(_ctx("job-p"), insights)
    assert len(signals) >= 2

    update_job("job-p", status=JOB_STATUS_CANCELLED)
    assert act.psychology_activity(_ctx("job-p"), insights) == []


def test_consistency_empty_insights_returns_fallback() -> None:
    create_job("job-cons", request=_REQUEST)
    update_job("job-cons", status=JOB_STATUS_RUNNING)

    signals = act.consistency_activity(_ctx("job-cons"), [])

    assert len(signals) == 1
    assert signals[0]["signal"] == "Cross-interview theme consistency"


def test_consistency_skips_when_terminal() -> None:
    create_job("job-cons2", request=_REQUEST)
    update_job("job-cons2", status=JOB_STATUS_CANCELLED)

    assert act.consistency_activity(_ctx("job-cons2"), []) == []


def test_viability_returns_recommendation() -> None:
    create_job("job-v", request=_REQUEST)
    update_job("job-v", status=JOB_STATUS_RUNNING)
    signals = [{"signal": "s", "confidence": 0.6, "evidence": []}]

    rec = act.viability_activity(_ctx("job-v"), signals, insight_count=3)

    assert rec["verdict"] in {
        "insufficient_evidence",
        "needs_more_validation",
        "promising_with_risks",
    }


def test_viability_zero_evidence_when_terminal() -> None:
    create_job("job-vc", request=_REQUEST)
    update_job("job-vc", status=JOB_STATUS_CANCELLED)

    rec = act.viability_activity(_ctx("job-vc"), [], insight_count=5)

    # Terminal → deterministic zero-evidence verdict (no LLM spend).
    assert rec["verdict"] == "insufficient_evidence"


def test_scripts_returns_list_and_skips_when_terminal() -> None:
    create_job("job-s", request=_REQUEST)
    update_job("job-s", status=JOB_STATUS_RUNNING)

    scripts = act.scripts_activity(_ctx("job-s"))
    assert isinstance(scripts, list) and scripts

    update_job("job-s", status=JOB_STATUS_CANCELLED)
    assert act.scripts_activity(_ctx("job-s")) == []


# ---------------------------------------------------------------------------
# report_progress / mark_failed
# ---------------------------------------------------------------------------


def test_report_progress_active_vs_terminal() -> None:
    create_job("job-pr", request=_REQUEST)
    update_job("job-pr", status=JOB_STATUS_RUNNING)

    assert act.report_progress_activity("job-pr", "ingest", 10) is True
    assert get_job("job-pr")["progress"] == 10

    update_job("job-pr", status=JOB_STATUS_CANCELLED)
    assert act.report_progress_activity("job-pr", "viability", 75) is False


def test_mark_failed_writes_failed_and_noops_when_terminal() -> None:
    create_job("job-mf", request=_REQUEST)
    update_job("job-mf", status=JOB_STATUS_RUNNING)

    act.mark_failed_activity("job-mf", "boom")
    job = get_job("job-mf")
    assert job["status"] == JOB_STATUS_FAILED
    assert job["error"] == "boom"

    # Already cancelled → mark_failed is a no-op (never clobbers a cancel).
    create_job("job-mf2", request=_REQUEST)
    update_job("job-mf2", status=JOB_STATUS_CANCELLED)
    act.mark_failed_activity("job-mf2", "boom")
    assert get_job("job-mf2")["status"] == JOB_STATUS_CANCELLED


# ---------------------------------------------------------------------------
# finalize
# ---------------------------------------------------------------------------


def _finalize_inputs():
    insights = [
        {
            "source": "s",
            "user_jobs": ["j"],
            "pain_points": [],
            "desired_outcomes": [],
            "direct_quotes": [],
        }
    ]
    signals = [
        {"signal": "a", "confidence": 0.5, "evidence": []},
        {"signal": "b", "confidence": 0.5, "evidence": []},
    ]
    recommendation = {
        "verdict": "needs_more_validation",
        "confidence": 0.5,
        "rationale": [],
        "suggested_next_experiments": [],
    }
    scripts = ["a script"]
    return insights, signals, recommendation, scripts


def test_finalize_assembles_and_writes_completed() -> None:
    create_job("job-fin", request=_REQUEST)
    update_job("job-fin", status=JOB_STATUS_RUNNING)
    insights, signals, recommendation, scripts = _finalize_inputs()

    out = act.finalize_activity(
        _ctx("job-fin", approved=True), insights, signals, recommendation, scripts
    )

    assert out == {"job_id": "job-fin"}
    job = get_job("job-fin")
    assert job["status"] == JOB_STATUS_COMPLETED
    assert job["result"]["status"] == "ready_for_execution"
    assert job["result"]["proposed_research_scripts"] == ["a script"]


def test_finalize_human_decision_branch_when_not_approved() -> None:
    create_job("job-fin2", request=_REQUEST)
    update_job("job-fin2", status=JOB_STATUS_RUNNING)
    insights, signals, recommendation, scripts = _finalize_inputs()

    act.finalize_activity(
        _ctx("job-fin2", approved=False), insights, signals, recommendation, scripts
    )

    assert get_job("job-fin2")["result"]["status"] == "needs_human_decision"


def test_finalize_short_circuits_on_clean_terminal_without_write() -> None:
    create_job("job-fin3", request=_REQUEST)
    update_job("job-fin3", status=JOB_STATUS_CANCELLED)
    insights, signals, recommendation, scripts = _finalize_inputs()

    out = act.finalize_activity(_ctx("job-fin3"), insights, signals, recommendation, scripts)

    assert out == {"job_id": "job-fin3"}
    job = get_job("job-fin3")
    assert job["status"] == JOB_STATUS_CANCELLED
    assert "result" not in job


def test_finalize_raises_non_retryably_when_failed() -> None:
    create_job("job-fin4", request=_REQUEST)
    update_job("job-fin4", status=JOB_STATUS_FAILED)
    insights, signals, recommendation, scripts = _finalize_inputs()

    with pytest.raises(ApplicationError) as exc:
        act.finalize_activity(_ctx("job-fin4"), insights, signals, recommendation, scripts)
    assert exc.value.non_retryable is True


def test_finalize_raises_when_job_missing() -> None:
    insights, signals, recommendation, scripts = _finalize_inputs()
    with pytest.raises(RuntimeError, match="not found at finalize"):
        act.finalize_activity(_ctx("job-gone"), insights, signals, recommendation, scripts)


def test_finalize_does_not_write_completed_when_cancel_lands_during_assembly(monkeypatch) -> None:
    """A cancel that arrives while ``assemble`` runs must not be clobbered by a
    COMPLETED write (the second terminal short-circuit)."""
    create_job("job-fin5", request=_REQUEST)
    update_job("job-fin5", status=JOB_STATUS_RUNNING)
    insights, signals, recommendation, scripts = _finalize_inputs()

    def _assemble_then_cancel(self, *args, **kwargs):
        update_job("job-fin5", status=JOB_STATUS_CANCELLED)
        return None  # return value is unused — the post-assembly guard returns early

    monkeypatch.setattr(
        "market_research_team.orchestrator.MarketResearchOrchestrator.assemble",
        _assemble_then_cancel,
    )

    out = act.finalize_activity(_ctx("job-fin5"), insights, signals, recommendation, scripts)

    assert out == {"job_id": "job-fin5"}
    job = get_job("job-fin5")
    assert job["status"] == JOB_STATUS_CANCELLED
    assert "result" not in job
