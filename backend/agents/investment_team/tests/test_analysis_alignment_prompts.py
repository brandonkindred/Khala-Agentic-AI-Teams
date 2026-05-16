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
from pathlib import Path

import pytest

from investment_team.strategy_lab.agents.analysis import _PROMPT_DIR
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    FixedFractionSizing,
    Predicate,
    SMARef,
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
            lhs=SMARef(period=20),
            op="gt",
            rhs=SMARef(period=50),
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


_RENDERERS: dict[str, tuple[str, callable]] = {
    "prompt_analysis_win.txt": ("analysis_win", _render_win),
    "prompt_analysis_lose.txt": ("analysis_lose", _render_lose),
    "prompt_alignment_system.txt": ("alignment_system", _render_alignment_system),
}


@pytest.mark.parametrize(
    "golden_filename,label,renderer",
    [(f, label, fn) for f, (label, fn) in _RENDERERS.items()],
    ids=[label for _, (label, _) in _RENDERERS.items()],
)
def test_rendered_prompt_matches_golden(
    golden_filename: str, label: str, renderer: callable
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
        ("analysis_win", _render_win),
        ("analysis_lose", _render_lose),
    ],
)
def test_analysis_prompts_have_intended_not_enforced_label(label: str, renderer: callable) -> None:
    """Issue #528: prose rules must be labelled as intent, not enforced behaviour."""
    rendered = renderer()
    assert "may not all be machine-enforced" in rendered, (
        f"{label} prompt must label entry/exit rules as 'may not all be "
        "machine-enforced' (issue #528)."
    )
    assert "Intended entry rules" in rendered, (
        f"{label} prompt must use 'Intended entry rules' (issue #528)."
    )
    assert "Intended exit rules" in rendered, (
        f"{label} prompt must use 'Intended exit rules' (issue #528)."
    )


def test_alignment_prompt_splits_enforced_and_aspirational() -> None:
    """Issue #528: alignment prompt must distinguish enforced vs aspirational rules."""
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


@pytest.mark.parametrize(
    "label,renderer",
    [
        ("analysis_win", _render_win),
        ("analysis_lose", _render_lose),
        ("alignment_system", _render_alignment_system),
    ],
)
def test_prompts_do_not_use_mandatory_or_hard_rule_phrasing(label: str, renderer: callable) -> None:
    """Issue #528: drop 'mandatory' / 'hard rule' phrasing across all three prompts."""
    rendered = renderer().lower()
    forbidden = ["mandatory", "hard rule", "hard-enforced"]
    offenders = [phrase for phrase in forbidden if phrase in rendered]
    assert not offenders, (
        f"{label} prompt still contains forbidden phrasing {offenders} "
        "(issue #528 — prose rules are not engine-enforced)."
    )


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
