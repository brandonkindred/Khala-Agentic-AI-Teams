"""Diff-utility wiring into ``DesignAgent``'s revise-prompt path.

``diff_spec_or_full`` is a standalone, separately-tested utility (see
``test_strategy_lab_diff_format.py``). This file covers the wiring step,
mirroring ``test_strategy_lab_refinement_diff_wiring.py``'s coverage for the
code-diff wiring in ``RefinementAgent``: round 1 of ``revise()`` (no
previous round) still sends the full spec JSON, byte-identical to the
original prompt; round 2+ sends a compact structural diff against the
previous round's spec instead, and the resulting prompt is measurably
smaller than always resending the full spec would be. Also covers the
internal self-revision loop inside ``_with_self_review``, which diffs
round-over-round independently of the external ``revise()`` lineage.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from investment_team.models import RiskLimits, StrategySpec
from investment_team.strategy_lab.agents import design as mod
from investment_team.strategy_lab.agents.design import DesignAgent
from investment_team.strategy_lab.agents.design_review import CritiqueIssue, SpecCritique
from investment_team.strategy_lab.spec_dsl import EntryRule, IndicatorRef, Predicate


def _spec(strategy_id: str = "strat-design-diff-wiring", **overrides: Any) -> StrategySpec:
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
    """Captures each round's fully-rendered ``user_prompt`` without touching the LLM.

    Overriding ``_invoke_and_parse`` isolates exactly the piece under test
    here — the prompt ``revise()`` builds before ever reaching the LLM call
    — while still exercising ``revise()``'s real diff-wiring logic and
    round-over-round instance state (mirrors
    ``test_strategy_lab_refinement_diff_wiring.py``'s ``_CapturingRefinementAgent``).
    """

    def __init__(self, scripted_dicts: List[Dict[str, Any]]) -> None:
        super().__init__()
        self.captured_prompts: List[str] = []
        self._scripted_dicts = list(scripted_dicts)

    def _invoke_and_parse(self, system_prompt: str, user_prompt: str) -> Tuple[Dict[str, Any], str]:
        self.captured_prompts.append(user_prompt)
        idx = len(self.captured_prompts) - 1
        return dict(self._scripted_dicts[idx]), f"round {idx + 1} rationale"


class _RaisingOnceDesignAgent(DesignAgent):
    """Raises on its first call, then succeeds — simulates a transient LLM failure."""

    def __init__(self, dict_after_success: Dict[str, Any]) -> None:
        super().__init__()
        self.captured_prompts: List[str] = []
        self._dict_after_success = dict_after_success
        self._raise_next = True

    def _invoke_and_parse(self, system_prompt: str, user_prompt: str) -> Tuple[Dict[str, Any], str]:
        self.captured_prompts.append(user_prompt)
        if self._raise_next:
            self._raise_next = False
            raise ValueError("simulated transient failure")
        return dict(self._dict_after_success), "fixed"


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


def _small_entry_rules(n: int = 10) -> List[EntryRule]:
    return _big_entry_rules(n)


def _big_entry_rule_dicts(n: int = 40) -> List[Dict[str, Any]]:
    """Plain-dict counterpart of :func:`_big_entry_rules` for raw ``strategy_dict`` fixtures.

    The internal self-revision loop works on plain JSON-serializable dicts
    (the LLM response shape), never pydantic model instances, so tests that
    feed a ``strategy_dict`` straight into ``_with_self_review`` need this
    instead of :func:`_big_entry_rules`.
    """
    return [{"side": "long", "when": {"lhs": "sma", "op": "<", "rhs": float(i)}} for i in range(n)]


def test_round_one_revise_sends_full_spec_unchanged() -> None:
    """No previous round exists yet, so the prompt carries the full spec JSON."""
    spec = _spec()
    revised_dict = {"entry_rules": _small_entry_rules()}
    agent = _CapturingDesignAgent(scripted_dicts=[revised_dict])

    agent.revise(spec, _critique(), skip_self_review=True)

    assert len(agent.captured_prompts) == 1
    prompt = agent.captured_prompts[0]
    full_json = spec.model_dump_json(indent=2, exclude={"strategy_code"})
    assert f"```json\n{full_json}\n```" in prompt
    assert "structural diff" not in prompt


def test_round_two_revise_sends_smaller_diff_than_full_resend_would_be() -> None:
    """Round 2's revise() prompt is smaller than an always-resend-full-spec baseline."""
    spec_round1 = _spec(entry_rules=_big_entry_rules())
    revised_dict_round1 = {"entry_rules": _big_entry_rules(), "hypothesis": "h2"}
    revised_dict_round2 = {"entry_rules": _big_entry_rules(), "hypothesis": "h3"}
    agent = _CapturingDesignAgent(scripted_dicts=[revised_dict_round1, revised_dict_round2])

    strategy_dict_1, _ = agent.revise(spec_round1, _critique(), skip_self_review=True)
    assert strategy_dict_1["hypothesis"] == "h2"

    spec_round2 = spec_round1.model_copy(update={"hypothesis": "h2"})
    agent.revise(spec_round2, _critique(), skip_self_review=True)

    assert len(agent.captured_prompts) == 2
    round2_prompt = agent.captured_prompts[1]

    # Baseline: what round 2's prompt would look like if it always resent the
    # full spec instead of diffing against round 1's spec.
    current_dict = spec_round2.model_dump(exclude={"strategy_code"})
    full_json = spec_round2.model_dump_json(indent=2, exclude={"strategy_code"})
    baseline_section = mod._render_spec_section(full_json, full_json, is_diff=False)
    diffed = mod.diff_spec_or_full(spec_round1.model_dump(exclude={"strategy_code"}), current_dict)
    actual_section = mod._render_spec_section(full_json, diffed, is_diff=True)
    baseline_prompt = round2_prompt.replace(actual_section, baseline_section)

    assert actual_section in round2_prompt
    assert len(round2_prompt) < len(baseline_prompt)
    assert "structural diff" in round2_prompt
    assert "hypothesis" in round2_prompt


def test_round_over_round_state_diffs_against_immediately_prior_round() -> None:
    """Round 3's revise() diffs against round 2's spec, not round 1's."""
    spec_round1 = _spec(entry_rules=_big_entry_rules(), hypothesis="h1")
    revised_round1 = {"entry_rules": _big_entry_rules(), "hypothesis": "h2"}
    revised_round2 = {"entry_rules": _big_entry_rules(), "hypothesis": "h3"}
    revised_round3 = {"entry_rules": _big_entry_rules(), "hypothesis": "h4"}
    agent = _CapturingDesignAgent(scripted_dicts=[revised_round1, revised_round2, revised_round3])

    agent.revise(spec_round1, _critique(), skip_self_review=True)
    spec_round2 = spec_round1.model_copy(update={"hypothesis": "h2"})
    agent.revise(spec_round2, _critique(), skip_self_review=True)
    spec_round3 = spec_round1.model_copy(update={"hypothesis": "h3"})
    agent.revise(spec_round3, _critique(), skip_self_review=True)

    assert agent._previous_round_spec is not None
    assert agent._previous_round_spec["hypothesis"] == "h3"
    assert len(agent.captured_prompts) == 3
    round3_prompt = agent.captured_prompts[2]
    # Diffed against round2 -> round3, so the h2 -> h3 edit shows up.
    assert "h3" in round3_prompt
    # The older round1 -> round2 edit (h1 -> h2) must not resurface as a
    # changed-value pair in this round's diff.
    assert "changed: hypothesis: 'h1' -> 'h2'" not in round3_prompt


def test_run_resets_previous_round_spec_for_a_new_lineage() -> None:
    """A fresh run() call must not let a new lineage diff against a prior attempt's spec.

    ``StrategyLabOrchestrator`` reuses one ``DesignAgent`` instance across
    multiple design attempts within a single ``run_cycle()`` call (e.g. after
    a prior attempt raised ``SpecImplementabilityError`` and
    ``_run_design_attempt`` re-entered with a new ``strategy_id``). Without
    resetting ``_previous_round_spec`` in ``run()``, the new lineage's first
    ``revise()`` call would diff against the unrelated prior attempt's spec.
    """
    import os

    stale_spec = _spec(
        strategy_id="prior-attempt", entry_rules=_big_entry_rules(), hypothesis="stale"
    )
    seed_revised_dict = {"entry_rules": _big_entry_rules(), "hypothesis": "stale-revised"}
    generated_dict = {"entry_rules": _big_entry_rules(), "hypothesis": "fresh-lineage"}
    revised_dict = {"entry_rules": _big_entry_rules(), "hypothesis": "fresh-lineage-revised"}

    class _CapturingDesignAgentWithRun(_CapturingDesignAgent):
        def _self_review(self, strategy_dict: Dict[str, Any]) -> SpecCritique:  # pragma: no cover
            raise AssertionError("self-review should not be reached in this test")

    agent = _CapturingDesignAgentWithRun(
        scripted_dicts=[seed_revised_dict, generated_dict, revised_dict]
    )
    # Seed stale diff state, as if a prior design attempt's revise() had run
    # on this same (reused) instance.
    agent.revise(stale_spec, _critique(), skip_self_review=True)
    assert agent._previous_round_spec is not None

    original = os.environ.get("STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED")
    os.environ["STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED"] = "false"
    try:
        agent.run(prior_records=[])
    finally:
        if original is None:
            os.environ.pop("STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED", None)
        else:
            os.environ["STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED"] = original

    assert agent._previous_round_spec is None

    fresh_spec = _spec(
        strategy_id="new-attempt", entry_rules=_big_entry_rules(), hypothesis="fresh-lineage"
    )
    agent.revise(fresh_spec, _critique(), skip_self_review=True)

    revise_prompt = agent.captured_prompts[-1]
    full_json = fresh_spec.model_dump_json(indent=2, exclude={"strategy_code"})
    assert f"```json\n{full_json}\n```" in revise_prompt
    assert "structural diff" not in revise_prompt
    assert "stale" not in revise_prompt


def test_failed_revise_invocation_does_not_advance_diff_state() -> None:
    """A failed revise() call must not corrupt the next round's diff base."""
    spec = _spec()
    fixed_dict = {"entry_rules": _small_entry_rules()}
    agent = _RaisingOnceDesignAgent(dict_after_success=fixed_dict)

    try:
        agent.revise(spec, _critique(), skip_self_review=True)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass

    assert agent._previous_round_spec is None

    # Retry with the SAME prior_spec, mirroring an orchestrator fallback.
    agent.revise(spec, _critique(), skip_self_review=True)

    assert len(agent.captured_prompts) == 2
    retry_prompt = agent.captured_prompts[1]
    full_json = spec.model_dump_json(indent=2, exclude={"strategy_code"})
    assert f"```json\n{full_json}\n```" in retry_prompt
    assert "structural diff" not in retry_prompt


def test_small_dict_uses_full_json_when_diff_would_render_larger() -> None:
    """A small dict's rendered diff can exceed the rendered full-JSON section's size.

    ``diff_spec_or_full`` alone compares raw diff/JSON lengths, but the
    actual prompt section adds a preamble to the diff only — for a small
    dict with a single-field edit, that overhead can flip which is
    smaller (here: a 33-char raw diff loses to a 125-char full JSON once
    the ~230-char explanatory preamble is added). The self-revision loop
    (exercised directly here via ``_with_self_review``, sidestepping
    ``revise()``'s bulky full ``StrategySpec`` — every real spec carries
    enough required fields that its full JSON always dwarfs a one-line
    diff) must compare the rendered sections, not trust
    ``diff_spec_or_full``'s raw comparison alone.
    """
    strategy_dict = {"entry_rules": [{"a": 1}, {"a": 2}, {"a": 3}], "hypothesis": "h1"}
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
    ]
    self_review_calls = {"n": 0}
    revision_dicts = [
        {"entry_rules": [{"a": 1}, {"a": 2}, {"a": 3}], "hypothesis": "h2"},
        {"entry_rules": [{"a": 1}, {"a": 2}, {"a": 3}], "hypothesis": "h3"},
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

    import os

    original = os.environ.get("STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS")
    os.environ["STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS"] = "2"
    try:
        agent = _StubbedSelfReviewAgent()
        agent._with_self_review(strategy_dict, "initial rationale")
    finally:
        if original is None:
            os.environ.pop("STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS", None)
        else:
            os.environ["STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS"] = original

    assert len(agent.captured_prompts) == 2
    second_prompt = agent.captured_prompts[1]
    full_json = json.dumps(revision_dicts[0], indent=2, sort_keys=True)

    # Round 2 must still render the full dict: the rendered diff section
    # (preamble + fences) is larger than the rendered full section for a
    # single-field change on this small dict, even though the raw diff
    # text alone is shorter than the raw full JSON.
    assert f"```json\n{full_json}\n```" in second_prompt
    assert "structural diff" not in second_prompt


def test_self_revision_loop_round_two_diffs_against_round_one() -> None:
    """The internal self-revision loop diffs round 2 against round 1's spec, not full."""
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

    import os

    original = os.environ.get("STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS")
    os.environ["STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS"] = "2"
    try:
        agent = _StubbedSelfReviewAgent()
        agent._with_self_review(strategy_dict, "initial rationale")
    finally:
        if original is None:
            os.environ.pop("STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS", None)
        else:
            os.environ["STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS"] = original

    assert len(agent.captured_prompts) == 2
    first_prompt, second_prompt = agent.captured_prompts
    assert "structural diff" not in first_prompt  # round 1: no previous self-revision
    assert "structural diff" in second_prompt  # round 2: diffs against round 1's output
    assert "h1" in second_prompt
