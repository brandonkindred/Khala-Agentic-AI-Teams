"""Regression guard for issue #528.

Strategy Lab analysis prompts used to flow prose `entry_rules` / `exit_rules`
straight into the LLM and frame them as engine-enforced ("Entry rules:" /
"Exit rules:"). Together with alignment_system.md asserting that "each trade's
exit_date / exit_price is taken because at least one exit rule fires", that
encouraged the LLM to label any deviation as a critical "mandatory" violation
- even though the engine never enforced the prose. Issue #528 is the stopgap
prompt rewording until the structured-rule DSL work (#537) lands.

This test pins:

1. The rendered prompt text against checked-in goldens in
   ``tests/golden/snapshots/`` so drift is caught loudly.
2. The new "intended, not enforced" wording on the analysis prompts.
3. The new enforced-vs-aspirational split on alignment_system.md.
4. The absence of "mandatory" / "hard rule" phrasing in any of them.

Renderer reuse: the helper imports ``format_rules_for_prompt`` and
``format_sizing_rule`` from ``strategy_lab.spec_dsl`` (the same callers the
``AnalysisAgent`` and ``TradeAlignmentAgent`` use) so a refactor of the
formatters is caught by this test.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from investment_team.strategy_lab.agents.alignment import (
    AlignmentIssue,
    TradeAlignmentReport,
)
from investment_team.strategy_lab.agents.analysis import (
    _PROMPT_DIR,
    _SELF_REVIEW_PROMPT,
    _format_alignment_status_section,
    format_misalignment_prefix,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    FixedFractionSizing,
    IndicatorRef,
    Predicate,
    StopLossRule,
    TakeProfitRule,
    format_rules_for_prompt,
    format_sizing_rule,
)

_GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "snapshots"


def _render_inputs() -> dict[str, object]:
    """Stable inputs for the analysis prompt goldens.

    Kept inline (not on disk) so the test file is the single source of truth
    for the fixture; the .txt goldens are derived artifacts.
    """
    entry = EntryRule(
        side="long",
        when=Predicate(
            lhs=IndicatorRef(name="sma", params={"period": 20}),
            op=">",
            rhs=IndicatorRef(name="sma", params={"period": 50}),
        ),
    )
    exits = [TakeProfitRule(pct=0.05), StopLossRule(pct=0.02)]
    sizing = FixedFractionSizing(fraction=0.01)
    return {
        "asset_class": "equity",
        "hypothesis": "Trend-followers profit when SMA(20) crosses above SMA(50).",
        "signal_definition": "SMA(20) > SMA(50)",
        "entry_rules": format_rules_for_prompt([entry]),
        "exit_rules": format_rules_for_prompt(exits),
        "sizing_rules": format_sizing_rule(sizing),
        "rationale": "Validate SMA-cross trend following on the equity sleeve.",
        "annualized_return_pct": 12.5,
        "total_return_pct": 25.0,
        "sharpe_ratio": 1.45,
        "max_drawdown_pct": 8.3,
        "win_rate_pct": 55.0,
        "profit_factor": 1.8,
        "volatility_pct": 14.2,
        "simulated_trades_section": (
            "Trade 1: BUY AAA on 2024-01-15 @ 100.00, SELL on 2024-01-25 @ 102.50 (+2.5%)\n"
            "Trade 2: BUY BBB on 2024-02-01 @ 50.00, SELL on 2024-02-11 @ 49.00 (-2.0%)"
        ),
        "alignment_status_section": _format_alignment_status_section(
            TradeAlignmentReport(aligned=True)
        ),
    }


def _misaligned_report() -> TradeAlignmentReport:
    """Stable misaligned fixture for the analysis-prompt golden tests."""
    return TradeAlignmentReport(
        aligned=False,
        rationale=(
            "Two trades skipped the stop-loss; one trade entered a symbol "
            "outside the spec universe."
        ),
        issues=[
            AlignmentIssue(
                rule_type="exit_rules",
                severity="critical",
                description="stop-loss did not fire on trade #1 despite -8% drawdown",
                affected_trades=[1],
            ),
            AlignmentIssue(
                rule_type="universe",
                severity="warning",
                description="trade #2 used symbol BBB which is outside the spec universe",
                affected_trades=[2],
            ),
        ],
    )


def _render_win() -> str:
    template = (_PROMPT_DIR / "analysis_win.md").read_text(encoding="utf-8")
    return template.format(**_render_inputs())


def _render_lose() -> str:
    template = (_PROMPT_DIR / "analysis_lose.md").read_text(encoding="utf-8")
    inputs = _render_inputs()
    inputs.update(
        {
            "annualized_return_pct": 3.1,
            "total_return_pct": 4.2,
            "sharpe_ratio": 0.42,
            "max_drawdown_pct": 18.7,
            "win_rate_pct": 38.0,
            "profit_factor": 0.95,
            "volatility_pct": 22.4,
        }
    )
    return template.format(**inputs)


def _render_lose_misaligned() -> str:
    template = (_PROMPT_DIR / "analysis_lose.md").read_text(encoding="utf-8")
    inputs = _render_inputs()
    inputs.update(
        {
            "annualized_return_pct": 3.1,
            "total_return_pct": 4.2,
            "sharpe_ratio": 0.42,
            "max_drawdown_pct": 18.7,
            "win_rate_pct": 38.0,
            "profit_factor": 0.95,
            "volatility_pct": 22.4,
            "alignment_status_section": _format_alignment_status_section(_misaligned_report()),
        }
    )
    return template.format(**inputs)


_PromptRenderer = Callable[[], str]

_RENDERERS: dict[str, tuple[str, _PromptRenderer]] = {
    "prompt_analysis_win.txt": ("analysis_win", _render_win),
    "prompt_analysis_lose.txt": ("analysis_lose", _render_lose),
    "prompt_analysis_lose_misaligned.txt": (
        "analysis_lose_misaligned",
        _render_lose_misaligned,
    ),
}


@pytest.mark.parametrize(
    "golden_filename,label,renderer",
    [pytest.param(filename, label, fn, id=label) for filename, (label, fn) in _RENDERERS.items()],
)
def test_rendered_prompt_matches_golden(
    golden_filename: str, label: str, renderer: _PromptRenderer
) -> None:
    """Rendered prompt MUST match the on-disk golden byte-for-byte.

    Regenerate after intentional wording changes by running the rendering
    helpers and writing each output to the matching ``_GOLDEN_DIR / <name>``.
    """
    expected = (_GOLDEN_DIR / golden_filename).read_text(encoding="utf-8")
    actual = renderer()
    assert actual == expected, (
        f"Rendered '{label}' prompt drifted from {golden_filename}. "
        "If the wording change was intentional, regenerate the golden."
    )


@pytest.mark.parametrize(
    "label,renderer",
    [
        pytest.param("analysis_win", _render_win, id="analysis_win"),
        pytest.param("analysis_lose", _render_lose, id="analysis_lose"),
    ],
)
def test_analysis_prompts_label_entry_intent_and_exit_enforcement(
    label: str, renderer: _PromptRenderer
) -> None:
    """Issue #527: entry rules stay 'intended' prose; exit rules became
    engine-enforced once the structured DSL got wired into the bar loop.
    Issue #528's 'Intended entry rules' / 'may not all be machine-enforced'
    framing survives on the entry half so the LLM still doesn't police
    prose entry intent as a mandatory rule.
    """
    rendered = renderer()
    assert "Intended entry rules" in rendered, (
        f"{label} prompt must label entry rules as 'Intended entry rules' "
        "(carry-over from issue #528)."
    )
    assert "may not all be machine-enforced" in rendered, (
        f"{label} prompt must still label entry rules as 'may not all be "
        "machine-enforced' (carry-over from issue #528)."
    )
    assert "Engine-enforced exit rules" in rendered, (
        f"{label} prompt must label exit rules as 'Engine-enforced exit rules' "
        "(issue #527 — structured exit rules are now applied by the parent "
        "engine every bar)."
    )


@pytest.mark.parametrize(
    "label,renderer",
    [
        pytest.param("analysis_win", _render_win, id="analysis_win"),
        pytest.param("analysis_lose", _render_lose, id="analysis_lose"),
    ],
)
def test_analysis_prompts_carry_risk_model_framing(label: str, renderer: _PromptRenderer) -> None:
    """Both draft prompts must teach the correct risk model: deployed size is
    the per-trade capital at risk, post-entry safeguards are a separate
    dimension, and ``fraction × stop`` is never the per-trade-risk figure.

    Guards against the regression where the analysis LLM read the deployed
    fraction as a stop-multiplied "effective risk", called it "capital in
    play", and blamed weak returns on it.
    """
    rendered = renderer()
    assert "How to read the sizing line" in rendered, (
        f"{label} prompt must carry the sizing-interpretation block."
    )
    assert "capital DEPLOYED" in rendered, (
        f"{label} prompt must frame the sizing line as deployed capital at risk."
    )
    assert "deployed-fraction × stop is wrong" in rendered, (
        f"{label} prompt must forbid multiplying the stop into sizing."
    )
    assert "SEPARATE per-trade-outcome dimension" in rendered, (
        f"{label} prompt must require analyzing post-entry safeguards separately."
    )
    # The sizing line is not always a fixed fraction: vol-target and
    # fixed-notional must be described so the agent doesn't read a vol target
    # or a dollar notional as "a fraction of the account". A fixed notional is
    # capped by the position limit, so it must be framed as a target, not the
    # exact deployed amount.
    assert "vol-target" in rendered, f"{label} prompt must explain the vol-target sizing rendering."
    assert "capped by the position limit" in rendered, (
        f"{label} prompt must frame the fixed-notional sizing as capped, not exact."
    )
    # The exact per-trade deployed dollars are not in the evidence (dynamic
    # sizing, position cap, whole-share rounding), and exit reasons are not
    # labelled, so the prompt must keep both qualitative / evidence-conditional.
    assert "whole-share rounding" in rendered, (
        f"{label} prompt must warn that whole-share rounding can change deployed size."
    )
    assert "Attribute an exit to a specific rule only where the evidence supports it" in rendered, (
        f"{label} prompt must make exit attribution evidence-conditional."
    )


def test_lose_prompt_targets_conflation_not_genuine_small_sizing() -> None:
    """The losing-strategy prompt must block the reported failure mode — a
    stop-multiplied "effective risk" figure blamed for returns — WITHOUT
    suppressing the accurate observation that a genuinely small deployment can
    itself constrain returns (which would contradict the self-review prompt and
    omit a valid sizing failure).
    """
    rendered = _render_lose()
    assert 'stop-multiplied "effective risk" figure' in rendered
    assert (
        "limited deployment constrained returns is a legitimate, accurate explanation" in rendered
    )
    # The over-broad blanket ban on "too little capital in play" must be gone.
    assert '"too little capital in play"' not in rendered


def test_self_review_prompt_includes_sizing_as_source_fact() -> None:
    """The self-review source-of-truth block must carry the sizing line.

    The ``1a`` risk-model check asks the reviewer to confirm the deployed
    position size, so the reviewer needs the actual sizing as ground truth —
    otherwise it can only infer it from the (possibly mistaken) draft and
    cannot catch a hallucinated fraction.
    """
    assert "Sizing / risk: {sizing_rules}" in _SELF_REVIEW_PROMPT


def test_self_review_check_preserves_accurate_low_capital_statement() -> None:
    """The risk-model check must strike only the stop-multiplied conflation and
    the misattribution of returns — NOT the accurate statement that a small
    deployment is genuinely small capital at risk (deployed size IS capital at
    risk under this model).
    """
    assert "must be preserved" in _SELF_REVIEW_PROMPT
    assert "genuinely small deployment is small capital at risk" in _SELF_REVIEW_PROMPT
    # The over-broad clause that rejected the accurate equation must be gone.
    assert "equates a low deployed size with low capital-at-risk" not in _SELF_REVIEW_PROMPT


def test_self_review_check_handles_vol_target_and_capped_notional_sizing() -> None:
    """The self-review check must not equate the "Sizing / risk" line with the
    deployed size for every rule: "vol-target X%" is a target annual volatility
    (deployed amount dynamic) and "$Y per trade" is capped by the position
    limit, so the reviewer must not read either as the exact capital at risk.
    """
    assert "capped by the position limit" in _SELF_REVIEW_PROMPT
    # Fixed-fraction is also nominal (lot rounding / cap can move it), so the
    # check must not read any of the three renderings as the exact capital.
    assert "(nominal, before whole-share lot rounding and the position cap)" in _SELF_REVIEW_PROMPT
    assert (
        'do NOT read "risk X% per trade", "vol-target X%", or "$Y per trade" '
        "as the exact capital at risk" in _SELF_REVIEW_PROMPT
    )
    # The review prompt lacks per-trade position_value / risk limits, so the
    # check must forbid asserting an exact figure rather than verify one.
    assert "an exact deployed-capital figure is not derivable" in _SELF_REVIEW_PROMPT


def test_analysis_system_prompt_carries_risk_model_and_no_forbidden_phrasing() -> None:
    """The analysis system prompt deepens the quant + veteran-trader persona
    and embeds the risk model. It is not snapshot-pinned, so assert its
    content directly (and that no forbidden enforcement phrasing crept in)."""
    text = (_PROMPT_DIR / "analysis_system.md").read_text(encoding="utf-8")
    lowered = text.lower()
    assert "veteran" in lowered, "system prompt must deepen the veteran-trader persona."
    assert "per-trade capital at risk" in text
    assert "deployed-fraction × stop" in text
    assert "low effective risk" in text  # the framing must name and forbid the bad reading
    # The deployed-size = capital-at-risk principle must cover all sizing
    # variants, not just fixed-fraction "% of the account"; a fixed notional is
    # capped by the position limit, so it is framed as a target, and a
    # vol-target line is a volatility target, not a deployed fraction.
    assert "vol-target X%" in text
    assert "capped by the position limit" in text
    assert "X% is NOT a deployed fraction" in text
    # Fixed-fraction is the nominal/target deployment before lot rounding, not
    # an exact deployed fraction (whole-share rounding can exceed it).
    assert "nominal deployed fraction before any whole-share lot rounding" in text
    # Trailing stops ratchet from the running extreme, not a move off entry.
    assert "ratchets from the running high/low" in text
    # Exact per-trade deployed dollars are not derivable from the rendered line.
    assert "whole-share rounding" in text
    for phrase in ("mandatory", "hard rule", "hard-enforced"):
        assert phrase not in lowered, f"system prompt must not use forbidden phrasing {phrase!r}."


@pytest.mark.parametrize(
    "label,renderer",
    [
        pytest.param("analysis_win", _render_win, id="analysis_win"),
        pytest.param("analysis_lose", _render_lose, id="analysis_lose"),
    ],
)
def test_prompts_do_not_use_mandatory_or_hard_rule_phrasing(
    label: str, renderer: _PromptRenderer
) -> None:
    """Drop 'mandatory' / 'hard rule' phrasing across the analysis prompts.

    The alignment fix-proposer prompt (``alignment_propose_fix.md``) is
    grounded in structured findings and has no surface where this
    phrasing could re-enter, so it's not covered here.
    """
    rendered = renderer().lower()
    forbidden = ["mandatory", "hard rule", "hard-enforced"]
    offenders = [phrase for phrase in forbidden if phrase in rendered]
    assert not offenders, (
        f"{label} prompt still contains forbidden phrasing {offenders} "
        "(prose rules are not engine-enforced)."
    )


def test_no_mandatory_when_hold_days_mismatch() -> None:
    """Regression for the specific failure mode that motivated issue #528.

    Original behaviour: an analysis run on a strategy that authored "exit after
    10 bars" but produced trades with hold periods of 3-7 bars led the LLM to
    emit narratives like "the mandatory 10-bar time exit was violated in six of
    the seven recorded trades". The new prompt wording makes the prose
    explicitly 'intended', so the word 'mandatory' simply cannot appear in the
    template's text envelope. This test guards the template itself.
    """
    inputs = _render_inputs()
    # Force a mismatched hold-days narrative into the trade evidence to make
    # sure the template doesn't accidentally re-introduce "mandatory" via the
    # evidence channel.
    inputs["simulated_trades_section"] = (
        "Trade 1: BUY AAA on 2024-01-15, SELL on 2024-01-18 (hold 3 bars)\n"
        "Trade 2: BUY BBB on 2024-02-01, SELL on 2024-02-05 (hold 4 bars)"
    )
    template = (_PROMPT_DIR / "analysis_lose.md").read_text(encoding="utf-8")
    rendered = template.format(**inputs)
    # The template itself must not introduce "mandatory" phrasing.
    assert not re.search(r"\bmandatory\b", rendered, flags=re.IGNORECASE), (
        "analysis_lose.md re-introduced 'mandatory' wording (issue #528)."
    )


# ---------------------------------------------------------------------------
# Issue #532: alignment status threaded into analysis prompts
# ---------------------------------------------------------------------------


def test_alignment_section_empty_when_no_report() -> None:
    """Issue #532: callers that don't pass an ``alignment_report`` get an
    empty section so legacy behaviour is preserved byte-for-byte."""
    assert _format_alignment_status_section(None) == ""


def test_aligned_section_contains_clean_affirmation() -> None:
    """Issue #532: ``aligned=True`` produces a one-line audit-clean
    affirmation rather than the disclaimer block, so winning runs aren't
    accidentally injected with a misleading misalignment notice."""
    rendered = _format_alignment_status_section(TradeAlignmentReport(aligned=True))
    assert "## Alignment status" in rendered
    assert "audit clean" in rendered
    assert "did not faithfully implement" not in rendered
    assert "DO NOT make causal claims" not in rendered


def test_misaligned_section_contains_disclaimer_and_issues() -> None:
    """Issue #532: ``aligned=False`` must surface the disclaimer verbatim,
    enumerate every audit issue, and forbid causal claims about the design.
    Guards the safety-rail wording so refactors of the section don't
    silently delete it.
    """
    report = _misaligned_report()
    rendered = _format_alignment_status_section(report)
    assert "TRADES DID NOT IMPLEMENT THE SPEC" in rendered
    assert (
        "The executed trades did not faithfully implement the specification; "
        "interpretation is preliminary." in rendered
    )
    for issue in report.issues:
        assert issue.description in rendered
        assert issue.severity in rendered
        assert issue.rule_type in rendered
    assert report.rationale in rendered
    assert "DO open the narrative with the disclaimer above" in rendered
    assert "DO NOT make causal claims about strategy design" in rendered


def test_misaligned_section_handles_empty_issue_list() -> None:
    """Issue #532: even a degenerate ``aligned=False`` report with no
    enumerated issues must still produce the disclaimer block (so a
    fail-closed alignment retry path can't slip a confident narrative
    through)."""
    rendered = _format_alignment_status_section(TradeAlignmentReport(aligned=False))
    assert "TRADES DID NOT IMPLEMENT THE SPEC" in rendered
    assert "The executed trades did not faithfully implement the specification" in rendered
    assert "aligned=False with no enumerated issues" in rendered


@pytest.mark.parametrize(
    "label,template_file",
    [
        pytest.param("analysis_win", "analysis_win.md", id="analysis_win"),
        pytest.param("analysis_lose", "analysis_lose.md", id="analysis_lose"),
    ],
)
def test_analysis_templates_expose_alignment_placeholder(label: str, template_file: str) -> None:
    """Issue #532: both analysis prompt templates must expose the
    ``{alignment_status_section}`` placeholder so the orchestrator can
    inject the audit verdict + issues into the LLM context. Without this
    placeholder the alignment_status arg is silently dropped on the floor.
    """
    template = (_PROMPT_DIR / template_file).read_text(encoding="utf-8")
    assert "{alignment_status_section}" in template, (
        f"{label} template missing {{alignment_status_section}} placeholder (issue #532)."
    )


# ---------------------------------------------------------------------------
# format_misalignment_prefix: shared between the agent fallback and the
# orchestrator-level analysis-phase exception handler so the disclaimer can't
# disappear via either fallback path (PR #584 Codex review).
# ---------------------------------------------------------------------------


def test_misalignment_prefix_empty_for_none() -> None:
    assert format_misalignment_prefix(None) == ""


def test_misalignment_prefix_empty_for_aligned() -> None:
    assert format_misalignment_prefix(TradeAlignmentReport(aligned=True)) == ""


def test_misalignment_prefix_lists_disclaimer_and_issues() -> None:
    """Misaligned reports must emit the disclaimer first, then the
    enumerated issues — anything else risks the prefix being silently
    paraphrased away by downstream consumers."""
    report = _misaligned_report()
    prefix = format_misalignment_prefix(report)
    lines = prefix.split("\n")
    assert lines[0] == (
        "The executed trades did not faithfully implement the "
        "specification; interpretation is preliminary."
    )
    assert "Alignment issues:" in lines
    for issue in report.issues:
        assert any(
            line == f"- [{issue.severity}] {issue.rule_type}: {issue.description}" for line in lines
        ), f"Issue {issue.description!r} missing or malformed in prefix."


def test_misalignment_prefix_handles_empty_issue_list() -> None:
    """A degenerate ``aligned=False`` report with no enumerated issues
    must still emit the disclaimer (so fail-closed alignment retries
    can't slip through with a clean prefix)."""
    prefix = format_misalignment_prefix(TradeAlignmentReport(aligned=False))
    assert prefix.startswith("The executed trades did not faithfully implement the specification")
    assert "Alignment issues:" not in prefix
