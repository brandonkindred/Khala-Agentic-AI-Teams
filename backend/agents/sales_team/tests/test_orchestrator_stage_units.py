"""Tests for the per-prospect stage units + ``build_run_context``.

The Temporal per-prospect activities delegate to these ``SalesPodOrchestrator``
methods, so they are the shared seam between thread mode and Temporal mode.
Two properties matter and are covered here:

1. Each ``<stage>_one`` performs the agent call + wrap and **raises** on error
   (the activity relies on this to surface failures to Temporal; the thread
   path's ``_one`` closure catches and skips).
2. ``build_run_context`` derives the same request-scoped fields ``run()`` used.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sales_team import orchestrator as orch_mod
from sales_team.models import (
    BANTScore,
    CloseType,
    ClosingStrategy,
    ClosingStrategyBody,
    DiscoveryPlan,
    DiscoveryPlanBody,
    IdealCustomerProfile,
    MEDDICScore,
    NurtureSequence,
    NurtureSequenceBody,
    Prospect,
    ProspectDossier,
    QualificationScore,
    QualificationScoreBody,
    SalesPipelineRequest,
    SPINQuestions,
)
from sales_team.orchestrator import build_run_context


@pytest.fixture(autouse=True)
def _dummy_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agents resolve their LLM client at construction; the dummy harness makes
    ``SalesPodOrchestrator()`` constructible without a configured provider."""
    monkeypatch.setenv("LLM_PROVIDER", "dummy")


@pytest.fixture
def request_obj() -> SalesPipelineRequest:
    return SalesPipelineRequest(
        product_name="ProductX",
        value_proposition="Save 20% on outbound time",
        icp=IdealCustomerProfile(industry=["SaaS"]),
        company_context="We are a startup",
        case_study_snippets=["Won Acme 30% lift"],
    )


@pytest.fixture
def ctx(request_obj: SalesPipelineRequest):
    return build_run_context(request_obj, "job-1", insights_ctx="INSIGHTS")


@pytest.fixture
def orch(monkeypatch: pytest.MonkeyPatch) -> orch_mod.SalesPodOrchestrator:
    o = orch_mod.SalesPodOrchestrator()
    for slot in (
        "prospector",
        "outreach",
        "qualifier",
        "nurture",
        "discovery",
        "proposal",
        "closer",
        "coach",
        "outreach_critic",
        "proposal_critic",
    ):
        setattr(o, slot, MagicMock())
    return o


_PROSPECT = Prospect(id="prs_1", company_name="Acme Corp")


# ---------------------------------------------------------------------------
# build_run_context
# ---------------------------------------------------------------------------


def test_build_run_context_derives_request_fields(request_obj):
    c = build_run_context(request_obj, "job-9", insights_ctx="INS")
    assert c.job_id == "job-9"
    assert c.product == "ProductX"
    assert c.vp == "Save 20% on outbound time"
    assert c.company_context == "We are a startup"
    assert c.cases == "Won Acme 30% lift"
    assert c.entry == request_obj.entry_stage
    assert c.insights_ctx == "INS"
    assert '"industry"' in c.icp_json  # icp serialized to json


def test_build_run_context_defaults_to_noop_update(request_obj):
    c = build_run_context(request_obj, "job-9")
    assert c.update is orch_mod._noop_update
    c.update("stage", 10)  # no-op, must not raise


# ---------------------------------------------------------------------------
# qualify_one
# ---------------------------------------------------------------------------


def test_qualify_one_wraps_body_with_prospect(orch, ctx):
    body = QualificationScoreBody(
        bant=BANTScore(budget=3, authority=3, need=3, timeline=3),
        meddic=MEDDICScore(),
        recommended_action="advance",
    )
    orch.qualifier.qualify.return_value = body
    score = orch.qualify_one(_PROSPECT, ctx)
    assert isinstance(score, QualificationScore)
    assert score.prospect is _PROSPECT
    assert score.recommended_action == "advance"
    # insights + product threaded from ctx
    args = orch.qualifier.qualify.call_args.args
    assert args[1] == "ProductX" and args[4] == "INSIGHTS"


def test_qualify_one_raises_on_agent_error(orch, ctx):
    orch.qualifier.qualify.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        orch.qualify_one(_PROSPECT, ctx)


# ---------------------------------------------------------------------------
# nurture_one / discovery_one / close_one
# ---------------------------------------------------------------------------


def test_nurture_one_wraps_body(orch, ctx):
    orch.nurture.build_sequence.return_value = NurtureSequenceBody()
    seq = orch.nurture_one(_PROSPECT, ctx)
    assert isinstance(seq, NurtureSequence)
    assert seq.prospect is _PROSPECT


def test_discovery_one_uses_qual_json_or_empty(orch, ctx):
    orch.discovery.prepare.return_value = DiscoveryPlanBody(spin_questions=SPINQuestions())
    plan = orch.discovery_one(_PROSPECT, None, ctx)
    assert isinstance(plan, DiscoveryPlan)
    # no qualification => empty json object passed to the agent
    assert orch.discovery.prepare.call_args.args[1] == "{}"
    # dossier defaults to None => forwarded as None, no new failure mode
    assert orch.discovery.prepare.call_args.kwargs["dossier"] is None


def test_discovery_one_passes_dossier_through(orch, ctx):
    orch.discovery.prepare.return_value = DiscoveryPlanBody(spin_questions=SPINQuestions())
    dossier = ProspectDossier(
        dossier_id="d1",
        prospect_id="prs_1",
        full_name="J",
        current_title="VP",
        current_company="Acme",
        executive_summary="s",
        confidence=0.9,
    )
    plan = orch.discovery_one(_PROSPECT, None, ctx, dossier)
    assert isinstance(plan, DiscoveryPlan)
    assert orch.discovery.prepare.call_args.kwargs["dossier"] is dossier


def test_close_one_wraps_body(orch, ctx):
    orch.closer.develop_strategy.return_value = ClosingStrategyBody(
        recommended_close_technique=CloseType.ASSUMPTIVE
    )
    strat = orch.close_one(_PROSPECT, None, ctx)
    assert isinstance(strat, ClosingStrategy)
    assert orch.closer.develop_strategy.call_args.args[1] == "{}"  # no proposal => "{}"


def test_close_one_raises_on_agent_error(orch, ctx):
    orch.closer.develop_strategy.side_effect = ValueError("nope")
    with pytest.raises(ValueError, match="nope"):
        orch.close_one(_PROSPECT, None, ctx)


# ---------------------------------------------------------------------------
# outreach_one / proposal_one delegate to the critic-gated helpers
# ---------------------------------------------------------------------------


def test_outreach_one_delegates_to_critic_helper(orch, ctx):
    sentinel = object()
    orch._generate_outreach_with_critic = MagicMock(return_value=sentinel)
    dossier = ProspectDossier(
        dossier_id="d1",
        prospect_id="prs_1",
        full_name="J",
        current_title="VP",
        current_company="Acme",
        executive_summary="s",
        confidence=0.9,
    )
    out = orch.outreach_one(_PROSPECT, dossier, ctx)
    assert out is sentinel
    call = orch._generate_outreach_with_critic.call_args
    assert call.args[0] is _PROSPECT and call.args[1] is dossier
    assert call.kwargs["max_refinements"] == ctx.config.critic_max_refinements


def test_proposal_one_delegates_to_critic_helper(orch, ctx):
    sentinel = object()
    orch._generate_proposal_with_critic = MagicMock(return_value=sentinel)
    out = orch.proposal_one(_PROSPECT, None, None, ctx)
    assert out is sentinel
    call = orch._generate_proposal_with_critic.call_args
    assert call.args[0] is _PROSPECT
    assert call.kwargs["max_refinements"] == ctx.config.critic_max_refinements


# ---------------------------------------------------------------------------
# Thread-mode _run_* still skips a failing prospect (behaviour preserved)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Lazy specialist agents (cached_property) + idempotent outcome recording
# ---------------------------------------------------------------------------


def test_agents_resolve_lazily_cache_and_are_overridable():
    """Every specialist is a lazy ``cached_property``: it resolves on first
    access, caches (same instance twice), and stays assignable for tests."""
    from sales_team.agents import ProspectorAgent

    o = orch_mod.SalesPodOrchestrator()
    slots = (
        "prospector",
        "outreach",
        "qualifier",
        "nurture",
        "discovery",
        "proposal",
        "closer",
        "coach",
        "decision_maker_mapper",
        "dossier_builder",
        "learning_engine",
        "outreach_critic",
        "proposal_critic",
    )
    for slot in slots:
        agent = getattr(o, slot)
        assert agent is not None
        assert getattr(o, slot) is agent  # cached
    assert isinstance(o.prospector, ProspectorAgent)

    o.prospector = "sentinel"  # instance dict wins over the descriptor
    assert o.prospector == "sentinel"


def test_record_prospecting_outcomes_is_idempotent(monkeypatch):
    """Deterministic outcome ids make a replay overwrite the same store files
    instead of double-recording (which would skew learning stats)."""
    ids: list[list[str]] = []

    def _capture(outcome):
        ids[-1].append(outcome.outcome_id)
        return outcome

    monkeypatch.setattr("sales_team.orchestrator.record_stage_outcome", _capture)
    prospects = [Prospect(id="prs_1", company_name="A"), Prospect(id="prs_2", company_name="B")]

    for _ in range(2):
        ids.append([])
        orch_mod.record_prospecting_outcomes(prospects, "job-xyz")

    assert ids[0] == ids[1]  # same ids both runs
    assert len(set(ids[0])) == 2  # distinct per prospect


def test_run_qualification_skips_failing_prospect(orch, ctx):
    """The stage's ``_one`` closure still catches and drops a failing prospect,
    so a single failure does not abort the whole stage."""
    orch.qualifier.qualify.side_effect = RuntimeError("boom")
    assert orch._run_qualification(ctx, [_PROSPECT]) == []
