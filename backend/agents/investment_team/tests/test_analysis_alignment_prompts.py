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

from investment_team.strategy_lab.agents.analysis import _PROMPT_DIR
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    FixedFractionSizing,
    IndicatorRef,
    Predicate,
    StopLossRule,
    TimeStopRule,
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
    exits = [TimeStopRule(n_bars=10), StopLossRule(pct=0.02)]
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
    }


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


def _render_alignment_system() -> str:
    return (_PROMPT_DIR / "alignment_system.md").read_text(encoding="utf-8")


_PromptRenderer = Callable[[], str]

_RENDERERS: dict[str, tuple[str, _PromptRenderer]] = {
    "prompt_analysis_win.txt": ("analysis_win", _render_win),
    "prompt_analysis_lose.txt": ("analysis_lose", _render_lose),
    "prompt_alignment_system.txt": ("alignment_system", _render_alignment_system),
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


def test_alignment_prompt_splits_enforced_and_aspirational() -> None:
    """Issue #528 introduced the enforced vs aspirational split. Issue #527
    promotes structured ``exit_rules`` from the aspirational side into the
    enforced section; both section headers must remain so the LLM still
    sees the two-bucket framing for the rules that *are* aspirational
    (entry intent, SignalExitRule).
    """
    rendered = _render_alignment_system()
    assert "Enforced rules" in rendered, (
        "alignment_system.md must introduce 'Enforced rules' section (issue #528)."
    )
    assert "Aspirational rules" in rendered, (
        "alignment_system.md must introduce 'Aspirational rules' section (issue #528)."
    )
    # Severity guidance above the JSON output must reserve `critical` for enforced rules.
    assert "Reserve `severity: critical`" in rendered, (
        "alignment_system.md must reserve `severity: critical` for enforced rules (issue #528)."
    )


def test_alignment_prompt_marks_exit_rules_engine_enforced() -> None:
    """Issue #527: alignment prompt must describe ``exit_rules`` as engine-
    enforced rather than aspirational prose. Guards the prompt's role-in-
    pipeline framing so the LLM doesn't down-weight critical exit-rule
    violations the deterministic conformance gate flags.
    """
    rendered = _render_alignment_system()
    assert (
        "engine evaluates after every bar" in rendered or "evaluates after every bar" in rendered
    ), (
        "alignment_system.md must state the engine evaluates structured exit "
        "rules every bar (issue #527)."
    )
    assert "exit_rule_conformance" in rendered, (
        "alignment_system.md must reference the deterministic "
        "``exit_rule_conformance`` gate (issue #527)."
    )


@pytest.mark.parametrize(
    "label,renderer",
    [
        pytest.param("analysis_win", _render_win, id="analysis_win"),
        pytest.param("analysis_lose", _render_lose, id="analysis_lose"),
        pytest.param("alignment_system", _render_alignment_system, id="alignment_system"),
    ],
)
def test_prompts_do_not_use_mandatory_or_hard_rule_phrasing(
    label: str, renderer: _PromptRenderer
) -> None:
    """Issue #528: drop 'mandatory' / 'hard rule' phrasing across all three prompts."""
    rendered = renderer().lower()
    forbidden = ["mandatory", "hard rule", "hard-enforced"]
    offenders = [phrase for phrase in forbidden if phrase in rendered]
    assert not offenders, (
        f"{label} prompt still contains forbidden phrasing {offenders} "
        "(issue #528 — prose rules are not engine-enforced)."
    )


def test_alignment_prompt_keeps_severity_and_rule_type_enums_intact() -> None:
    """Issue #528 stops short of touching the alignment JSON schema. The
    severity/rule_type enums must stay exactly as the orchestrator's parsers
    expect, otherwise alignment-loop tests start failing. Guard the literal
    enum strings inside the JSON example block.
    """
    rendered = _render_alignment_system()
    assert '"severity": "info" | "warning" | "critical"' in rendered, (
        "alignment_system.md must keep the severity enum as info|warning|critical."
    )
    assert (
        '"rule_type": "entry_rules" | "exit_rules" | "sizing_rules" | '
        '"risk_limits" | "universe" | "direction"' in rendered
    ), "alignment_system.md must keep the rule_type enum unchanged."


def test_alignment_prompt_drops_all_count_as_misalignment() -> None:
    """The old wording asserted exit-rule deviation 'all count as misalignment';
    issue #528 explicitly downgrades that."""
    rendered = _render_alignment_system()
    assert "all count as misalignment" not in rendered, (
        "alignment_system.md must not assert that prose-rule deviations "
        "'all count as misalignment' (issue #528)."
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
