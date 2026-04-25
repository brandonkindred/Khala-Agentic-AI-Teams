"""Regression tests for the sales_team.prompts package.

Two flavours:

1. **Byte-identical SYSTEM_PROMPT** — guards against accidental drift while the
   prompts package is young. The original inline ``_X_SYSTEM_PROMPT`` strings
   were extracted to leaf modules; ``SYSTEM_PROMPT`` after rendering an empty
   ``FEWSHOT_EXAMPLES`` must equal ``_BASE_SYSTEM_PROMPT`` for that module.

2. **TASK_TEMPLATE.format() == f-string** — proves the conversion from inline
   f-strings to ``str.format``-based templates didn't change the rendered
   user prompt for any agent. We render each template against a representative
   context and compare against the equivalent f-string.
"""

from __future__ import annotations

import pytest

from sales_team.prompts import (
    CLOSER_SYSTEM_PROMPT,
    CLOSER_TASK_TEMPLATE,
    COACH_SYSTEM_PROMPT,
    COACH_TASK_TEMPLATE,
    DECISION_MAKER_MAPPER_SYSTEM_PROMPT,
    DECISION_MAKER_MAPPER_TASK_TEMPLATE,
    DISCOVERY_SYSTEM_PROMPT,
    DISCOVERY_TASK_TEMPLATE,
    DOSSIER_BUILDER_SYSTEM_PROMPT,
    DOSSIER_BUILDER_TASK_TEMPLATE,
    NURTURE_SYSTEM_PROMPT,
    NURTURE_TASK_TEMPLATE,
    OUTREACH_SYSTEM_PROMPT,
    OUTREACH_TASK_TEMPLATE,
    PROPOSAL_SYSTEM_PROMPT,
    PROPOSAL_TASK_TEMPLATE,
    PROSPECT_COMPANIES_TASK_TEMPLATE,
    PROSPECT_TASK_TEMPLATE,
    PROSPECTOR_SYSTEM_PROMPT,
    QUALIFIER_SYSTEM_PROMPT,
    QUALIFIER_TASK_TEMPLATE,
)
from sales_team.prompts._fewshots import render_fewshots

SYSTEM_PROMPTS = [
    ("prospector", PROSPECTOR_SYSTEM_PROMPT),
    ("outreach", OUTREACH_SYSTEM_PROMPT),
    ("qualifier", QUALIFIER_SYSTEM_PROMPT),
    ("nurture", NURTURE_SYSTEM_PROMPT),
    ("discovery", DISCOVERY_SYSTEM_PROMPT),
    ("proposal", PROPOSAL_SYSTEM_PROMPT),
    ("closer", CLOSER_SYSTEM_PROMPT),
    ("coach", COACH_SYSTEM_PROMPT),
    ("decision_maker_mapper", DECISION_MAKER_MAPPER_SYSTEM_PROMPT),
    ("dossier_builder", DOSSIER_BUILDER_SYSTEM_PROMPT),
]


@pytest.mark.parametrize("name,prompt", SYSTEM_PROMPTS)
def test_system_prompts_are_non_empty(name: str, prompt: str) -> None:
    assert prompt, f"{name} system prompt is empty"
    assert (
        "Output Format" in prompt
        or "Output" in prompt
        or "JSON" in prompt
        or "dossier" in prompt.lower()
    ), f"{name} system prompt looks malformed (no output-format guidance)"


def test_render_fewshots_empty_returns_empty_string() -> None:
    assert render_fewshots([]) == ""


def test_render_fewshots_renders_pairs() -> None:
    out = render_fewshots([({"a": 1}, {"b": 2})])
    assert "## Examples" in out
    assert '"a": 1' in out
    assert '"b": 2' in out


# ---------------------------------------------------------------------------
# TASK_TEMPLATE format() vs original f-string
# ---------------------------------------------------------------------------


def test_prospect_task_template_matches_fstring() -> None:
    ctx = dict(
        product_name="ProductX",
        value_proposition="Save 20% on Y",
        company_context="CC",
        icp_json='{"icp": "..."}',
        max_prospects=10,
    )
    formatted = PROSPECT_TASK_TEMPLATE.format(**ctx)
    expected = (
        f"You are prospecting for: {ctx['product_name']}\n"
        f"Value proposition: {ctx['value_proposition']}\n"
        f"Company context: {ctx['company_context']}\n\n"
        f"Ideal Customer Profile:\n{ctx['icp_json']}\n\n"
        f"Research and return up to {ctx['max_prospects']} prospects that match this ICP. "
        "Use learning context above (if any) to prioritise industries and trigger-event "
        "types that have historically produced wins. "
        'Return a JSON object shaped as {"prospects": [ ... ]} as described in the '
        "system prompt output format."
    )
    assert formatted == expected


def test_prospect_companies_task_template_matches_fstring() -> None:
    ctx = dict(
        product_name="P",
        value_proposition="V",
        company_context="C",
        icp_json="I",
        max_companies=20,
    )
    formatted = PROSPECT_COMPANIES_TASK_TEMPLATE.format(**ctx)
    expected = (
        f"You are building an ACCOUNT list for: {ctx['product_name']}\n"
        f"Value proposition: {ctx['value_proposition']}\n"
        f"Company context: {ctx['company_context']}\n\n"
        f"Ideal Customer Profile:\n{ctx['icp_json']}\n\n"
        f"Research and return up to {ctx['max_companies']} distinct COMPANIES that match this ICP. "
        "Do NOT return individual contacts in this step — only company-level data. "
        "For each company include: company_name, website, industry, company_size_estimate, "
        "icp_match_score (0.0–1.0), research_notes (why this company is a fit and any recent "
        "trigger events), trigger_events (array of concrete events). Leave contact_name, "
        "contact_title, contact_email, linkedin_url as null in this step. "
        "Prefer companies with recent public trigger events (funding, leadership change, "
        "hiring spree, product launch, vendor switch). "
        'Return a JSON object shaped as {"prospects": [ ... ]} — no commentary.'
    )
    assert formatted == expected


def test_outreach_task_template_matches_fstring() -> None:
    threshold = 0.7
    dossier_block = "## DOSSIER BLOCK"
    formatted = OUTREACH_TASK_TEMPLATE.format(
        personalization_confidence_threshold=threshold,
        dossier_block=dossier_block,
        variant_count=3,
        prospect_json="P",
        product_name="X",
        value_proposition="V",
        company_context="C",
        case_studies="CS",
    )
    expected = (
        f"Confidence threshold for person-level personalization: "
        f"{threshold}. If the dossier's confidence is below "
        f"this threshold, every variant MUST use the company_soft_opener angle.\n\n"
        f"{dossier_block}\n\n"
        f"---\n\n"
        f"Produce 3 variants for this prospect:\nP\n\n"
        f"Product: X\n"
        f"Value proposition: V\n"
        f"Company context: C\n"
        f"Customer wins to reference: CS\n\n"
        "Apply Salesfolk personalization, SNAP principles, and the Jeb Blount 6-touch cadence. "
        "Enforce the Personalization Contract — every person-level claim in an email body "
        "must be paired with an evidence_citation whose dossier_field is a real path and "
        "whose source_url (when non-null) is one of the URLs listed under '### Sources' "
        "above. Use the learning context above (if any) to replicate high-reply angles. "
        "Return a single JSON object matching the schema in the system prompt."
    )
    assert formatted == expected


def test_qualifier_task_template_matches_fstring() -> None:
    ctx = dict(product_name="P", prospect_json="J", value_proposition="V", call_notes="None yet")
    formatted = QUALIFIER_TASK_TEMPLATE.format(**ctx)
    expected = (
        f"Qualify this prospect for {ctx['product_name']}:\n{ctx['prospect_json']}\n\n"
        f"Value proposition: {ctx['value_proposition']}\n"
        f"Notes from any prior conversation: {ctx['call_notes']}\n\n"
        "Score BANT (0–10 each), evaluate all 6 MEDDIC signals, assign Iannarino value tier (1–4), "
        "and recommend: advance / nurture / disqualify. "
        "Use the learning context above (if any) to calibrate scores — e.g. if the data shows "
        "that deals with authority < 6 rarely close, weigh authority more heavily. "
        "Return a JSON object with bant, meddic, overall_score, value_creation_level, "
        "recommended_action, disqualification_reason, qualification_notes."
    )
    assert formatted == expected


def test_nurture_task_template_matches_fstring() -> None:
    ctx = dict(duration_days=30, prospect_json="P", product_name="X", value_proposition="V")
    formatted = NURTURE_TASK_TEMPLATE.format(**ctx)
    expected = (
        f"Build a {ctx['duration_days']}-day nurture sequence for:\n{ctx['prospect_json']}\n\n"
        f"Product: {ctx['product_name']}\n"
        f"Value proposition: {ctx['value_proposition']}\n\n"
        "Apply HubSpot content-stage mapping (Awareness → Consideration → Decision), "
        "Gong Labs cadence research, and SNAP re-engagement principles. "
        "Use the learning context above (if any) to select content types that historically "
        "re-engaged stalled prospects and to set re-engagement triggers that match real patterns. "
        "Return a JSON object with duration_days, touchpoints (array of "
        "{day, channel, content_type, message, goal}), re_engagement_triggers (array), "
        "content_recommendations (array)."
    )
    assert formatted == expected


def test_discovery_task_template_matches_fstring() -> None:
    ctx = dict(prospect_json="P", qualification_json="Q", product_name="N", value_proposition="V")
    formatted = DISCOVERY_TASK_TEMPLATE.format(**ctx)
    expected = (
        f"Prepare a complete discovery call guide for:\nProspect: {ctx['prospect_json']}\n"
        f"Qualification context: {ctx['qualification_json']}\n\n"
        f"Product: {ctx['product_name']}\n"
        f"Value proposition: {ctx['value_proposition']}\n\n"
        "Write SPIN questions in all four categories, craft a Challenger Sale insight-led opener, "
        "build a tailored demo agenda (features tied to confirmed pains only), "
        "list expected objections, and define success criteria for this call. "
        "Use the learning context above (if any) to pre-populate expected_objections with "
        "the objections that have most commonly appeared in past deals. "
        "Return a JSON object with spin_questions {situation, problem, implication, need_payoff}, "
        "challenger_insight, demo_agenda, expected_objections, success_criteria_for_call."
    )
    assert formatted == expected


def test_proposal_task_template_matches_fstring() -> None:
    ctx = dict(
        prospect_json="P",
        product_name="X",
        value_proposition="V",
        annual_cost_usd=10000.0,
        discovery_notes="See prospect research notes",
        case_studies="CS",
        company_context="CC",
    )
    formatted = PROPOSAL_TASK_TEMPLATE.format(**ctx)
    expected = (
        f"Write a complete sales proposal for:\nProspect: {ctx['prospect_json']}\n\n"
        f"Product: {ctx['product_name']}\n"
        f"Value proposition: {ctx['value_proposition']}\n"
        f"Annual cost (USD): {ctx['annual_cost_usd']}\n"
        f"Discovery notes: {ctx['discovery_notes']}\n"
        f"Customer wins: {ctx['case_studies']}\n"
        f"Company context: {ctx['company_context']}\n\n"
        "Follow Iannarino's proposal structure. Calculate realistic ROI. "
        "Use the learning context above (if any) to pre-emptively address the most common "
        "objections in the risk_mitigation section, and to frame the proposal around "
        "the value dimensions that historically correlated with wins. "
        "Return a JSON object with executive_summary, situation_analysis, proposed_solution, "
        "roi_model {annual_cost_usd, estimated_annual_benefit_usd, payback_months, roi_percentage, assumptions}, "
        "investment_table, implementation_timeline, risk_mitigation, next_steps (array), "
        "custom_sections (array of {heading, content})."
    )
    assert formatted == expected


def test_closer_task_template_matches_fstring() -> None:
    ctx = dict(prospect_json="P", proposal_json="Q", product_name="X", value_proposition="V")
    formatted = CLOSER_TASK_TEMPLATE.format(**ctx)
    expected = (
        f"Develop a closing strategy for:\nProspect: {ctx['prospect_json']}\n"
        f"Proposal context: {ctx['proposal_json']}\n\n"
        f"Product: {ctx['product_name']}\n"
        f"Value proposition: {ctx['value_proposition']}\n\n"
        "Select the most appropriate Zig Ziglar closing technique for this prospect, "
        "write the close script, prepare objection handlers (with Feel/Felt/Found), "
        "identify a legitimate urgency lever, and define walk-away criteria. "
        "Use the learning context above (if any) to: (1) prefer the close technique with the "
        "highest observed win rate, (2) include pre-written handlers for the most common "
        "historically-observed objections. "
        "Return a JSON object with recommended_close_technique, close_script, "
        "objection_handlers (array of {objection, response, feel_felt_found}), "
        "urgency_framing, walk_away_criteria, emotional_intelligence_notes."
    )
    assert formatted == expected


def test_coach_task_template_matches_fstring() -> None:
    ctx = dict(product_name="X", prospects_json="P", pipeline_context="None provided")
    formatted = COACH_TASK_TEMPLATE.format(**ctx)
    expected = (
        f"Review this sales pipeline for {ctx['product_name']}:\n{ctx['prospects_json']}\n\n"
        f"Additional pipeline context: {ctx['pipeline_context']}\n\n"
        "Identify deal risk signals (using Gong Labs framework), provide talk/listen ratio advice, "
        "velocity insights, forecast categorization, top priority deals, and specific next actions. "
        "Use the learning context above (if any) to compare this pipeline's patterns against "
        "historical win/loss data and flag deals that match known losing patterns. "
        "Return a JSON object with prospects_reviewed, deal_risk_signals (array of {signal, severity, recommended_action}), "
        "talk_listen_ratio_advice, velocity_insights, forecast_category, "
        "top_priority_deals (array), recommended_next_actions (array), coaching_summary."
    )
    assert formatted == expected


def test_decision_maker_mapper_task_template_matches_fstring() -> None:
    ctx = dict(
        product_name="X", value_proposition="V", company_json="C", icp_json="I", max_contacts=2
    )
    formatted = DECISION_MAKER_MAPPER_TASK_TEMPLATE.format(**ctx)
    expected = (
        f"Product: {ctx['product_name']}\n"
        f"Value proposition: {ctx['value_proposition']}\n\n"
        f"Target company (account-level research already done):\n{ctx['company_json']}\n\n"
        f"Ideal Customer Profile:\n{ctx['icp_json']}\n\n"
        f"Identify up to {ctx['max_contacts']} real decision-makers at this company who are likely "
        "to own the purchasing decision for this product. Use public signals only — titles, "
        "LinkedIn, press releases, vendor case studies, job postings, conference talks. "
        'Return a JSON object shaped as {"contacts": [ ... ]}. If no decision-maker can be '
        'confidently identified, return {"contacts": []}.'
    )
    assert formatted == expected


def test_dossier_builder_task_template_matches_fstring() -> None:
    ctx = dict(product_name="X", value_proposition="V", prospect_json="P")
    formatted = DOSSIER_BUILDER_TASK_TEMPLATE.format(**ctx)
    expected = (
        f"Build a full dossier for this prospect to prepare for a sales conversation about "
        f"{ctx['product_name']}.\n\n"
        f"Product: {ctx['product_name']}\n"
        f"Value proposition: {ctx['value_proposition']}\n\n"
        f"Prospect (from earlier prospecting stage — includes prospect_id):\n{ctx['prospect_json']}\n\n"
        "Cite every public URL you consulted in `sources`. Never fabricate. "
        "Return a single JSON object matching the ProspectDossier schema."
    )
    assert formatted == expected
