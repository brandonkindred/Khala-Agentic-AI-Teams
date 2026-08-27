"""Diff-utility wiring into ``RefinementAgent``'s prompt-building path.

``diff_or_full`` is a standalone, separately-tested utility (see
``test_strategy_lab_diff_format.py``). This file covers the wiring step:
round 1 (no previous round) still sends the full strategy code, byte-
identical to the original prompt; round 2+ sends a compact diff against the
previous round's code instead, and the resulting prompt is measurably
smaller than always resending the full file would be.
"""

from __future__ import annotations

from typing import Any, Dict, List

from investment_team.models import RiskLimits, StrategySpec
from investment_team.strategy_lab.agents import refinement as mod
from investment_team.strategy_lab.agents.refinement import RefinementAgent


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-diff-wiring",
        authored_by="test",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        risk_limits=RiskLimits(),
    )


class _CapturingRefinementAgent(RefinementAgent):
    """Captures each round's fully-rendered ``user_prompt`` without touching the LLM.

    Overriding ``_invoke_and_parse`` (rather than stubbing the ``Agent``/
    transport layer, as the parse-retry tests do) isolates exactly the piece
    under test here — the prompt ``run()`` builds before ever reaching the
    LLM call — while still exercising ``run()``'s real diff-wiring logic and
    round-over-round instance state.
    """

    def __init__(self, scripted_codes: List[str]) -> None:
        super().__init__()
        self.captured_prompts: List[str] = []
        self._scripted_codes = list(scripted_codes)

    def _invoke_and_parse(
        self, system_prompt: str, user_prompt: str, failure_phase: str
    ) -> Dict[str, Any]:
        self.captured_prompts.append(user_prompt)
        idx = len(self.captured_prompts) - 1
        code = self._scripted_codes[idx]
        return {"strategy_code": code, "changes_made": f"round {idx + 1} fix"}


def _big_code(n_lines: int = 80) -> str:
    return "\n".join(f"line_{i} = {i}" for i in range(n_lines)) + "\n"


def test_round_one_sends_full_code_unchanged() -> None:
    """No previous round exists yet, so the prompt carries the full file."""
    original = _big_code()
    fixed = original.replace("line_40 = 40", "line_40 = 999")
    agent = _CapturingRefinementAgent(scripted_codes=[fixed])

    agent.run(
        spec=_spec(),
        code=original,
        failure_phase="execution",
        failure_details="boom",
    )

    assert len(agent.captured_prompts) == 1
    prompt = agent.captured_prompts[0]
    assert f"```python\n{original}\n```" in prompt
    assert "unified diff against the previous round" not in prompt


def test_round_two_sends_smaller_diff_than_full_resend_would_be() -> None:
    """Round 2's prompt is smaller than an always-resend-full-text baseline."""
    round1_code = _big_code()
    round2_code = round1_code.replace("line_40 = 40", "line_40 = 999")
    round3_code = round2_code.replace("line_60 = 60", "line_60 = 888")
    agent = _CapturingRefinementAgent(scripted_codes=[round2_code, round3_code])

    _, code_after_round1 = agent.run(
        spec=_spec(),
        code=round1_code,
        failure_phase="execution",
        failure_details="boom",
    )
    assert code_after_round1 == round2_code

    agent.run(
        spec=_spec(),
        code=round2_code,
        failure_phase="execution",
        failure_details="boom again",
    )

    assert len(agent.captured_prompts) == 2
    round2_prompt = agent.captured_prompts[1]

    # Baseline: what round 2's prompt would look like if it always resent the
    # full file instead of diffing against round 1's code.
    baseline_section = mod._render_code_section(round2_code, round2_code, is_diff=False)
    actual_section = mod._render_code_section(
        round2_code,
        mod.diff_or_full(round1_code, round2_code),
        is_diff=True,
    )
    baseline_prompt = round2_prompt.replace(actual_section, baseline_section)

    assert actual_section in round2_prompt
    assert len(round2_prompt) < len(baseline_prompt)
    assert "unified diff against the previous round" in round2_prompt
    assert "line_40" in round2_prompt
    assert "999" in round2_prompt


def test_round_over_round_state_diffs_against_immediately_prior_round() -> None:
    """Round 3 diffs against round 2's code, not round 1's."""
    round1_code = _big_code()
    round2_code = round1_code.replace("line_10 = 10", "line_10 = 111")
    round3_code = round2_code.replace("line_70 = 70", "line_70 = 777")
    round4_code = round3_code.replace("line_20 = 20", "line_20 = 222")
    agent = _CapturingRefinementAgent(scripted_codes=[round2_code, round3_code, round4_code])

    agent.run(spec=_spec(), code=round1_code, failure_phase="execution", failure_details="d1")
    agent.run(spec=_spec(), code=round2_code, failure_phase="execution", failure_details="d2")
    agent.run(spec=_spec(), code=round3_code, failure_phase="execution", failure_details="d3")

    assert agent._previous_round_code == round3_code
    assert len(agent.captured_prompts) == 3
    round3_prompt = agent.captured_prompts[2]
    # Diffed against round2 -> round3, so the line_70 edit shows up.
    assert "line_70" in round3_prompt
    assert "777" in round3_prompt
    # The older round1 -> round2 edit must not resurface.
    assert "line_10" not in round3_prompt
    assert "111" not in round3_prompt
