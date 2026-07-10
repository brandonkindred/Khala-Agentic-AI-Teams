"""Tests for the pure pipeline gating + routing helpers (``sales_team.routing``).

These functions are the single source of truth shared by the thread orchestrator
and the Temporal workflow, so they are exercised directly (no LLM, no I/O).
"""

from __future__ import annotations

import pytest

from sales_team.models import BANTScore, MEDDICScore, PipelineStage, Prospect, QualificationScore
from sales_team.routing import (
    PIPELINE_STAGE_ORDER,
    is_advance,
    is_disqualify,
    partition_qualified,
    stage_should_run,
)


def _qual(company: str, action: str) -> QualificationScore:
    return QualificationScore(
        prospect=Prospect(id=f"prs_{company}", company_name=company),
        bant=BANTScore(budget=3, authority=3, need=3, timeline=3),
        meddic=MEDDICScore(),
        overall_score=0.7,
        value_creation_level=2,
        recommended_action=action,
    )


# ---------------------------------------------------------------------------
# stage_should_run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stage,entry,expected",
    [
        (PipelineStage.PROSPECTING, PipelineStage.PROSPECTING, True),
        (PipelineStage.NEGOTIATION, PipelineStage.PROSPECTING, True),
        (PipelineStage.PROSPECTING, PipelineStage.OUTREACH, False),
        (PipelineStage.OUTREACH, PipelineStage.QUALIFICATION, False),
        (PipelineStage.DISCOVERY, PipelineStage.DISCOVERY, True),
        (PipelineStage.PROPOSAL, PipelineStage.DISCOVERY, True),
    ],
)
def test_stage_should_run_truth_table(stage, entry, expected):
    assert stage_should_run(stage, entry) is expected


def test_stage_should_run_accepts_raw_strings():
    """The workflow passes ``request["entry_stage"]`` as a raw string."""
    assert stage_should_run("qualification", "prospecting") is True
    assert stage_should_run("outreach", "discovery") is False


@pytest.mark.parametrize("terminal", [PipelineStage.CLOSED_WON, PipelineStage.CLOSED_LOST])
def test_stage_should_run_false_for_terminal_entry(terminal):
    """A terminal ``closed_*`` state is not a runnable stage — nothing runs."""
    for stage in PIPELINE_STAGE_ORDER:
        assert stage_should_run(stage, terminal) is False


def test_stage_should_run_false_for_unknown_stage():
    assert stage_should_run("not_a_stage", PipelineStage.PROSPECTING) is False


# ---------------------------------------------------------------------------
# is_advance / is_disqualify
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,advance,disqualify",
    [
        ("advance", True, False),
        ("ADVANCE to discovery", True, False),
        ("Advance now", True, False),
        ("nurture", False, False),
        ("disqualify", False, True),
        ("Disqualify — no budget", False, True),
        ("", False, False),
    ],
)
def test_advance_disqualify_predicates(action, advance, disqualify):
    assert is_advance(action) is advance
    assert is_disqualify(action) is disqualify


# ---------------------------------------------------------------------------
# partition_qualified
# ---------------------------------------------------------------------------


def test_partition_empty_returns_all_prospects():
    prospects = [Prospect(company_name="A"), Prospect(company_name="B")]
    nurture, qualified = partition_qualified([], prospects)
    assert nurture == []
    assert qualified == prospects


def test_partition_all_advance():
    scores = [_qual("A", "advance"), _qual("B", "advance to proposal")]
    nurture, qualified = partition_qualified(scores, [s.prospect for s in scores])
    assert nurture == []
    assert [p.company_name for p in qualified] == ["A", "B"]


def test_partition_mixed_routes_advance_nurture_disqualify():
    scores = [
        _qual("A", "advance"),
        _qual("B", "nurture"),
        _qual("C", "disqualify"),
    ]
    nurture, qualified = partition_qualified(scores, [s.prospect for s in scores])
    # advance -> qualified; nurture -> nurture; disqualify -> dropped from both
    assert [p.company_name for p in qualified] == ["A"]
    assert [p.company_name for p in nurture] == ["B"]


def test_partition_all_disqualify_drops_everything():
    scores = [_qual("A", "disqualify"), _qual("B", "disqualify")]
    nurture, qualified = partition_qualified(scores, [s.prospect for s in scores])
    assert nurture == []
    assert qualified == []


def test_partition_all_nurture():
    scores = [_qual("A", "nurture"), _qual("B", "nurture")]
    nurture, qualified = partition_qualified(scores, [s.prospect for s in scores])
    assert [p.company_name for p in nurture] == ["A", "B"]
    assert qualified == []
