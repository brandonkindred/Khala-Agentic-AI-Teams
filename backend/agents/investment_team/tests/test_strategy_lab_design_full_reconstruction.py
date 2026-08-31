"""``DesignAgent`` always sends the full spec, never a diff, in its revise prompts.

``_invoke_and_parse`` builds a fresh, history-free ``Agent`` per round
(deliberately, to avoid feeding back unparseable output), so a diff-only
"## Current Specification" section would leave the model with no independent
copy of the untouched prior content to reconstruct from — risking a silently
dropped or hallucinated field with no downstream cross-check against the true
prior ``StrategySpec``. This is the correctness reason ``DesignAgent`` does
NOT mirror ``RefinementAgent``'s code-diff wiring
(``test_strategy_lab_refinement_diff_wiring.py``); see
``SPEC_RECONSTRUCTION_FIDELITY.md`` for the full rationale. These tests lock
in the always-full-spec contract, both on the external ``revise()`` lineage
and the internal self-revision loop inside ``_with_self_review``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

import pytest

from investment_team.models import RiskLimits, StrategySpec
from investment_team.strategy_lab.agents.design import DesignAgent
from investment_team.strategy_lab.agents.design_review import CritiqueIssue, SpecCritique
from investment_team.strategy_lab.spec_dsl import EntryRule, IndicatorRef, Predicate


def _spec(strategy_id: str = "strat-design-full-reconstruction", **overrides: Any) -> StrategySpec:
    fields: Dict[str, Any] = {
        "strategy_id": strategy_id,
        "authored_by": "test",
        "asset_class": "stocks",
        "hypothesis": "h",
        "signal_definition": "s",
        "timeframe": "1d",
        "risk_limits": RiskLimits(),
    }
    fields.update(overrides)
    return StrategySpec(**fields)


def _critique(field: str = "entry_rules", description: str = "fix it") -> SpecCritique:
    return SpecCritique(
        ready=False,
        rationale="needs work",
        issues=[CritiqueIssue(field=field, description=description, suggested_fix="do X")],
    )


class _CapturingDesignAgent(DesignAgent):
    """Captures each round's fully-rendered ``user_prompt`` without touching the LLM."""

    def __init__(self, scripted_dicts: List[Dict[str, Any]]) -> None:
        super().__init__()
        self.captured_prompts: List[str] = []
        self._scripted_dicts = list(scripted_dicts)

    def _invoke_and_parse(self, system_prompt: str, user_prompt: str) -> Tuple[Dict[str, Any], str]:
        self.captured_prompts.append(user_prompt)
        idx = len(self.captured_prompts) - 1
        return dict(self._scripted_dicts[idx]), f"round {idx + 1} rationale"


def _big_entry_rules(n: int = 40) -> List[EntryRule]:
    return [
        EntryRule(
            side="long",
            when=Predicate(
                lhs=IndicatorRef(name="sma", params={"period": i + 2}), op="<", rhs=float(i)
            ),
        )
        for i in range(n)
    ]


def _big_entry_rule_dicts(n: int = 40) -> List[Dict[str, Any]]:
    """Plain-dict counterpart of :func:`_big_entry_rules` for raw ``strategy_dict`` fixtures."""
    return [{"side": "long", "when": {"lhs": "sma", "op": "<", "rhs": float(i)}} for i in range(n)]


def test_round_one_revise_sends_full_spec() -> None:
    spec = _spec()
    revised_dict = {"entry_rules": []}
    agent = _CapturingDesignAgent(scripted_dicts=[revised_dict])

    agent.revise(spec, _critique(), skip_self_review=True)

    assert len(agent.captured_prompts) == 1
    prompt = agent.captured_prompts[0]
    full_json = spec.model_dump_json(indent=2, exclude={"strategy_code"})
    assert f"```json\n{full_json}\n```" in prompt
    assert "structural diff" not in prompt
    assert not re.search(r"\bdiff\b", prompt, re.IGNORECASE)


def test_every_subsequent_round_still_sends_the_full_spec() -> None:
    """A field unchanged since round 1 must still appear verbatim in round 2's prompt.

    This is the actual reconstruction-fidelity guarantee: nothing here is
    left for the (history-free) model to infer from a diff.
    """
    spec_round1 = _spec(entry_rules=_big_entry_rules(), hypothesis="h1")
    revised_round1 = {"entry_rules": _big_entry_rules(), "hypothesis": "h2"}
    revised_round2 = {"entry_rules": _big_entry_rules(), "hypothesis": "h3"}
    agent = _CapturingDesignAgent(scripted_dicts=[revised_round1, revised_round2])

    agent.revise(spec_round1, _critique(), skip_self_review=True)
    spec_round2 = spec_round1.model_copy(update={"hypothesis": "h2"})
    agent.revise(spec_round2, _critique(), skip_self_review=True)

    assert len(agent.captured_prompts) == 2
    for prompt in agent.captured_prompts:
        assert "structural diff" not in prompt
        assert not re.search(r"\bdiff\b", prompt, re.IGNORECASE)

    round2_prompt = agent.captured_prompts[1]
    full_json_round2 = spec_round2.model_dump_json(indent=2, exclude={"strategy_code"})
    assert f"```json\n{full_json_round2}\n```" in round2_prompt


def test_failed_revise_invocation_still_sends_full_spec_on_retry() -> None:
    """A failed round leaves no diff state to corrupt — the retry still sends the full spec."""

    class _RaisingOnceDesignAgent(DesignAgent):
        def __init__(self, dict_after_success: Dict[str, Any]) -> None:
            super().__init__()
            self.captured_prompts: List[str] = []
            self._dict_after_success = dict_after_success
            self._raise_next = True

        def _invoke_and_parse(
            self, system_prompt: str, user_prompt: str
        ) -> Tuple[Dict[str, Any], str]:
            self.captured_prompts.append(user_prompt)
            if self._raise_next:
                self._raise_next = False
                raise ValueError("simulated transient failure")
            return dict(self._dict_after_success), "fixed"

    spec = _spec()
    fixed_dict = {"entry_rules": []}
    agent = _RaisingOnceDesignAgent(dict_after_success=fixed_dict)

    try:
        agent.revise(spec, _critique(), skip_self_review=True)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass

    agent.revise(spec, _critique(), skip_self_review=True)

    assert len(agent.captured_prompts) == 2
    retry_prompt = agent.captured_prompts[1]
    full_json = spec.model_dump_json(indent=2, exclude={"strategy_code"})
    assert f"```json\n{full_json}\n```" in retry_prompt
    assert "structural diff" not in retry_prompt


def test_self_revision_loop_sends_full_spec_every_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The internal self-revision loop never diffs against its own prior round either."""
    strategy_dict = {"entry_rules": _big_entry_rule_dicts(), "hypothesis": "h0"}

    critiques = [
        SpecCritique(
            ready=False,
            rationale="r1",
            issues=[CritiqueIssue(field="hypothesis", description="d1")],
        ),
        SpecCritique(
            ready=False,
            rationale="r2",
            issues=[CritiqueIssue(field="hypothesis", description="d2")],
        ),
        SpecCritique(ready=True, rationale="ok"),
    ]
    self_review_calls = {"n": 0}

    revision_dicts = [
        {"entry_rules": _big_entry_rule_dicts(), "hypothesis": "h1"},
        {"entry_rules": _big_entry_rule_dicts(), "hypothesis": "h2"},
    ]

    class _StubbedSelfReviewAgent(DesignAgent):
        def __init__(self) -> None:
            super().__init__()
            self.captured_prompts: List[str] = []

        def _self_review(self, strategy_dict: Dict[str, Any]) -> SpecCritique:
            idx = self_review_calls["n"]
            self_review_calls["n"] += 1
            return critiques[idx]

        def _invoke_and_parse(
            self, system_prompt: str, user_prompt: str
        ) -> Tuple[Dict[str, Any], str]:
            self.captured_prompts.append(user_prompt)
            idx = len(self.captured_prompts) - 1
            return dict(revision_dicts[idx]), f"self-revision {idx + 1}"

    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS", "2")
    agent = _StubbedSelfReviewAgent()
    agent._with_self_review(strategy_dict, "initial rationale")

    assert len(agent.captured_prompts) == 2
    first_full_json = json.dumps(strategy_dict, indent=2, sort_keys=True)
    second_full_json = json.dumps(revision_dicts[0], indent=2, sort_keys=True)
    first_prompt, second_prompt = agent.captured_prompts

    assert f"```json\n{first_full_json}\n```" in first_prompt
    assert f"```json\n{second_full_json}\n```" in second_prompt
    for prompt in agent.captured_prompts:
        assert "structural diff" not in prompt
        assert not re.search(r"\bdiff\b", prompt, re.IGNORECASE)
