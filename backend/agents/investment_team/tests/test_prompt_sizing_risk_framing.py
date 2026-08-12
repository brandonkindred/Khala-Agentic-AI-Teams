"""Wiring guard for the shared sizing/drawdown risk-framing reference block.

The sizing/drawdown policy — "the deployed fraction IS the loss cap, don't
multiply by stop, there is no max-drawdown constraint" — used to be
independently restated across the design and design-review system prompts.
The fix consolidates it into a canonical ``_sizing_risk_framing.md``
reference, mirroring the existing ``_stop_order_semantics.md`` shared-
fragment pattern. These tests assert the file exists with its load-bearing
content and that each consumer actually concatenates it — without invoking
any LLM.
"""

from __future__ import annotations

from pathlib import Path

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "strategy_lab" / "prompts"
_LOSS_CAP_MARKER = "per-trade loss cap"
_DRAWDOWN_MARKER = "NO max-drawdown"


def test_reference_block_exists_with_loadbearing_content() -> None:
    text = (_PROMPT_DIR / "_sizing_risk_framing.md").read_text(encoding="utf-8")
    assert _LOSS_CAP_MARKER in text
    assert _DRAWDOWN_MARKER in text
    lowered = text.lower()
    assert "fraction × stop" in lowered or "fraction x stop" in lowered
    assert "deployed" in lowered


def test_design_system_prompt_includes_block() -> None:
    from investment_team.strategy_lab.agents import design

    assert _LOSS_CAP_MARKER in design._get_design_system_prompt()
    assert _DRAWDOWN_MARKER in design._get_design_system_prompt()


def test_design_review_system_prompt_includes_block() -> None:
    from investment_team.strategy_lab.agents import design_review

    assert _LOSS_CAP_MARKER in design_review._get_system_prompt()
    assert _DRAWDOWN_MARKER in design_review._get_system_prompt()
