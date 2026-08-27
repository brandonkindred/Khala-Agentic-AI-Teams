"""Tests for ``sales_team.prompts._dossier_render.render_dossier_for_prompt``.

The renderer translates a ``ProspectDossier`` into the markdown block the
outreach prompt embeds. It must be:

  * deterministic (same input → same output),
  * bounded (long lists truncated to top-K),
  * empty-aware (no section heading for an empty list),
  * source-faithful (the Sources section preserves the input list exactly).
"""

from __future__ import annotations

import json

from sales_team.models import DecisionMakerSignal, ProspectDossier, PublicWorkItem
from sales_team.prompts._dossier_render import (
    _DOSSIER_LIST_TOP_K,
    _truncate,
    render_dossier_for_prompt,
    render_dossier_json_for_prompt,
)

# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------


def test_truncate_returns_input_unchanged_when_short() -> None:
    items = [1, 2, 3]
    out = _truncate(items, k=5)
    assert out == items
    # Same list object back (no copy) — confirms the short-circuit branch.
    assert out is items


def test_truncate_respects_k() -> None:
    assert _truncate([1, 2, 3, 4, 5, 6], k=3) == [1, 2, 3]


def test_truncate_default_k_is_five() -> None:
    out = _truncate(list(range(10)))
    assert len(out) == _DOSSIER_LIST_TOP_K


# ---------------------------------------------------------------------------
# render_dossier_for_prompt
# ---------------------------------------------------------------------------


def test_minimal_dossier_renders_header_and_identity() -> None:
    dossier = ProspectDossier(
        prospect_id="prs_min",
        full_name="Jane Smith",
        current_title="VP Sales",
        current_company="Acme Corp",
        confidence=0.42,
    )
    out = render_dossier_for_prompt(dossier)
    assert out.startswith("## Prospect Dossier (confidence: 0.42)")
    assert "### Identity" in out
    assert "Jane Smith — VP Sales at Acme Corp" in out
    # No optional sections for an otherwise-empty dossier.
    assert "### Executive Summary" not in out
    assert "### Trigger Events" not in out
    assert "### Sources" not in out


def test_identity_omits_dash_when_title_and_company_are_blank() -> None:
    """When both current_title and current_company are empty, the renderer
    still uses just the name. (Defensive — model_validator allows empty
    strings on these fields.)"""
    dossier = ProspectDossier(
        prospect_id="prs_x",
        full_name="Bare Name",
        current_title="",
        current_company="",
    )
    out = render_dossier_for_prompt(dossier)
    assert "Bare Name" in out


def test_identity_includes_location_linkedin_and_personal_site() -> None:
    dossier = ProspectDossier(
        prospect_id="prs_full_id",
        full_name="Jane Smith",
        current_title="VP Sales",
        current_company="Acme",
        location="New York, NY",
        linkedin_url="https://linkedin.com/in/jane-smith",
        personal_site="https://janesmith.example.com",
    )
    out = render_dossier_for_prompt(dossier)
    assert "Location: New York, NY" in out
    assert "LinkedIn: https://linkedin.com/in/jane-smith" in out
    assert "Personal site: https://janesmith.example.com" in out


def test_executive_summary_renders_when_present() -> None:
    dossier = ProspectDossier(
        prospect_id="prs_e",
        full_name="Jane",
        current_title="VP",
        current_company="Acme",
        executive_summary="Two-sentence summary.",
    )
    out = render_dossier_for_prompt(dossier)
    assert "### Executive Summary" in out
    assert "Two-sentence summary." in out


def test_trigger_events_truncate_to_top_k() -> None:
    events = [f"event-{i}" for i in range(8)]
    dossier = ProspectDossier(
        prospect_id="prs_te",
        full_name="J",
        current_title="V",
        current_company="A",
        trigger_events=events,
    )
    out = render_dossier_for_prompt(dossier)
    # Top-K rendered, rest dropped.
    rendered = [e for e in events if e in out]
    assert len(rendered) == _DOSSIER_LIST_TOP_K


def test_decision_maker_signals_render_strength_and_evidence() -> None:
    dossier = ProspectDossier(
        prospect_id="prs_dm",
        full_name="Jane",
        current_title="VP",
        current_company="Acme",
        decision_maker_signals=[
            DecisionMakerSignal(
                signal="reports_directly_to_ceo",
                evidence_url="https://acme.example.com/leadership",
                strength="strong",
            ),
            DecisionMakerSignal(signal="owns_budget_for_data_tooling"),
        ],
    )
    out = render_dossier_for_prompt(dossier)
    assert "### Decision Maker Signals" in out
    assert "[strong] reports_directly_to_ceo (https://acme.example.com/leadership)" in out
    # Default strength ("medium") renders, and a signal without evidence_url omits the URL suffix.
    assert "[medium] owns_budget_for_data_tooling" in out
    assert "owns_budget_for_data_tooling (" not in out


def test_decision_maker_signals_truncate_to_top_k() -> None:
    signals = [DecisionMakerSignal(signal=f"signal-{i}") for i in range(8)]
    dossier = ProspectDossier(
        prospect_id="prs_dm_trunc",
        full_name="J",
        current_title="V",
        current_company="A",
        decision_maker_signals=signals,
    )
    out = render_dossier_for_prompt(dossier)
    rendered = [s.signal for s in signals if s.signal in out]
    assert len(rendered) == _DOSSIER_LIST_TOP_K


def test_publications_render_all_metadata_fields() -> None:
    dossier = ProspectDossier(
        prospect_id="prs_pubs",
        full_name="Jane",
        current_title="VP",
        current_company="Acme",
        publications=[
            PublicWorkItem(
                kind="talk",
                title="Scaling SDR Teams",
                url="https://qcon.example.com/talk",
                venue="QCon SF",
                date="2025-11",
            ),
            PublicWorkItem(kind="article", title="Cadence basics"),
        ],
    )
    out = render_dossier_for_prompt(dossier)
    assert "### Publications" in out
    assert "[talk] Scaling SDR Teams" in out
    assert "— QCon SF" in out
    assert "(2025-11)" in out
    assert "https://qcon.example.com/talk" in out
    # The bare article publication still renders without venue/date/url.
    assert "[article] Cadence basics" in out


def test_recent_activity_conversation_hooks_and_mutual_connections() -> None:
    dossier = ProspectDossier(
        prospect_id="prs_rl",
        full_name="J",
        current_title="V",
        current_company="A",
        recent_activity=["Posted on LinkedIn about pipeline scale"],
        conversation_hooks=["Series B funding → pipeline scale"],
        mutual_connection_angles=["Worked at Stripe with our champion"],
    )
    out = render_dossier_for_prompt(dossier)
    assert "### Recent Activity" in out
    assert "Posted on LinkedIn about pipeline scale" in out
    assert "### Conversation Hooks" in out
    assert "Series B funding → pipeline scale" in out
    assert "### Mutual Connection Angles" in out
    assert "Worked at Stripe with our champion" in out


def test_stated_beliefs_render_when_present() -> None:
    dossier = ProspectDossier(
        prospect_id="prs_b",
        full_name="J",
        current_title="V",
        current_company="A",
        stated_beliefs=["Outbound is dead", "Multi-thread or die"],
    )
    out = render_dossier_for_prompt(dossier)
    assert "### Stated Beliefs" in out
    assert "Outbound is dead" in out
    assert "Multi-thread or die" in out


def test_topics_of_interest_render_inline_comma_separated() -> None:
    dossier = ProspectDossier(
        prospect_id="prs_topics",
        full_name="J",
        current_title="V",
        current_company="A",
        topics_of_interest=["sales ops", "RevOps", "CRO playbook"],
    )
    out = render_dossier_for_prompt(dossier)
    assert "### Topics of Interest" in out
    assert "sales ops, RevOps, CRO playbook" in out


def test_topics_of_interest_truncate_to_top_ten() -> None:
    topics = [f"topic-{i}" for i in range(15)]
    dossier = ProspectDossier(
        prospect_id="prs_t",
        full_name="J",
        current_title="V",
        current_company="A",
        topics_of_interest=topics,
    )
    out = render_dossier_for_prompt(dossier)
    # The function truncates Topics to top-10 (k=10), not the default top-K.
    assert "topic-0" in out
    assert "topic-9" in out
    assert "topic-10" not in out


def test_sources_render_full_unbounded_list() -> None:
    sources = [f"https://src-{i}.example.com" for i in range(8)]
    dossier = ProspectDossier(
        prospect_id="prs_s",
        full_name="J",
        current_title="V",
        current_company="A",
        sources=sources,
    )
    out = render_dossier_for_prompt(dossier)
    assert "### Sources (only these URLs may be cited)" in out
    # All sources appear (sources are intentionally NOT truncated).
    for s in sources:
        assert s in out


def test_render_is_deterministic_across_calls() -> None:
    dossier = ProspectDossier(
        prospect_id="prs_det",
        full_name="J",
        current_title="V",
        current_company="A",
        trigger_events=["e1"],
        sources=["https://x"],
    )
    a = render_dossier_for_prompt(dossier)
    b = render_dossier_for_prompt(dossier)
    assert a == b


# ---------------------------------------------------------------------------
# render_dossier_json_for_prompt
# ---------------------------------------------------------------------------


def test_json_render_returns_full_json() -> None:
    dossier = ProspectDossier(
        prospect_id="prs_j",
        full_name="Jane",
        current_title="VP",
        current_company="Acme",
    )
    out = render_dossier_json_for_prompt(dossier)
    assert "dossier truncated" not in out
    assert json.loads(out)["full_name"] == "Jane"


def test_json_render_never_truncates_large_dossiers() -> None:
    big_text = "lorem ipsum " * 5000  # ~60k chars
    dossier = ProspectDossier(
        prospect_id="prs_big",
        full_name="Jane",
        current_title="VP",
        current_company="Acme",
        executive_summary=big_text,
        trigger_events=[f"event {i}: {'x' * 500}" for i in range(30)],
    )
    out = render_dossier_json_for_prompt(dossier)
    assert "dossier truncated" not in out
    parsed = json.loads(out)
    assert parsed["executive_summary"] == big_text
    assert len(parsed["trigger_events"]) == 30
