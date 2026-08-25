"""Wiring guard for the shared stop-order semantics reference block.

The reported bug was the analysis/design agents mislabeling a trailing stop's
above-entry ratchet as a defect. The fix injects a canonical
``_stop_order_semantics.md`` reference into the analysis, design, and
design-review system prompts. These tests assert the file exists with its
load-bearing content and that each consumer actually concatenates it — without
invoking any LLM.
"""

from __future__ import annotations

from pathlib import Path

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "strategy_lab" / "prompts"
_MARKER = "NOT a defect"


def test_reference_block_exists_with_loadbearing_content() -> None:
    text = (_PROMPT_DIR / "_stop_order_semantics.md").read_text(encoding="utf-8")
    assert _MARKER in text
    lowered = text.lower()
    assert "trailing stop" in lowered
    assert "stop-limit" in lowered
    assert "stop-market" in lowered


def test_analysis_agent_loads_block() -> None:
    from investment_team.strategy_lab.agents import analysis

    assert _MARKER in analysis._STOP_ORDER_SEMANTICS
    assert _MARKER in analysis._ANALYSIS_SYSTEM_PROMPT


def test_design_system_prompts_include_block() -> None:
    from investment_team.strategy_lab.agents import design

    assert _MARKER in design._get_design_system_prompt()
    assert _MARKER in design._get_self_review_system_prompt()


def test_design_review_system_prompt_includes_block() -> None:
    from investment_team.strategy_lab.agents import design_review

    assert _MARKER in design_review._get_system_prompt()
