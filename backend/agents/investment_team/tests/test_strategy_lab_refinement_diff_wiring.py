"""``RefinementAgent.run``'s ``previous_code`` wiring into the prompt template.

Locks in the #7300 wiring of the #7286 ``diff_or_full`` utility into
``refinement.py``'s prompt-building path: with no ``previous_code`` the
``## Current Code`` block is the full file unchanged (byte-identical to
pre-#7300 behavior — the regression guard for existing refinement tests);
with a ``previous_code`` and a small incremental change, the block is a
unified diff strictly smaller than always resending the full file (the
required round-K>1 prompt-size benchmark); with a near-total-rewrite,
it falls back to the full file. Uses the same ``_StubClient`` /
structured-output monkeypatch pattern as
``test_strategy_lab_refinement_structured_output.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from investment_team.models import RiskLimits, StrategySpec
from investment_team.strategy_lab.agents import _structured_output as so_mod
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


class _FakeModel:
    def __init__(self, client: Any) -> None:
        self.client = client


class _StubClient:
    """Records the ``complete_json`` prompt and returns a fixed payload."""

    def __init__(self, result: Dict[str, Any]) -> None:
        self._result = result
        self.calls: List[Dict[str, Any]] = []

    def complete(self, prompt: str, **kwargs: Any) -> str:
        return "reasoning prose"

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append({"prompt": prompt, **kwargs})
        return self._result


def _run_and_capture_prompt(monkeypatch: pytest.MonkeyPatch, **run_kwargs: Any) -> str:
    stub_client = _StubClient({"strategy_code": "# fixed", "changes_made": "ok"})
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(stub_client))

    RefinementAgent().run(
        spec=_spec(), failure_phase="execution", failure_details="boom", **run_kwargs
    )

    assert len(stub_client.calls) == 1
    return stub_client.calls[0]["prompt"]


_ORIGINAL_CODE = "\n".join(f"line_{i} = {i}" for i in range(40))
_SMALL_CHANGE_CODE = _ORIGINAL_CODE.replace("line_5 = 5", "line_5 = 500")
_REWRITTEN_CODE = "\n".join(f"totally_different_{i} = object()" for i in range(40))


def test_no_previous_round_sends_full_code_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Round 1 (``previous_code=None``): behavior is byte-identical to
    pre-#7300 — the full current code, not a diff."""
    prompt = _run_and_capture_prompt(monkeypatch, code=_ORIGINAL_CODE, previous_code=None)

    assert _ORIGINAL_CODE in prompt
    assert "## Current Code\n```python" in prompt
    assert "(unified diff against the previous round — see Instructions)" not in prompt


def test_previous_round_with_small_change_sends_diff_smaller_than_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round K>1 with a small incremental change: prompt carries a unified
    diff and is smaller than the always-full-text baseline would have been."""
    diff_prompt = _run_and_capture_prompt(
        monkeypatch, code=_SMALL_CHANGE_CODE, previous_code=_ORIGINAL_CODE
    )
    full_prompt = _run_and_capture_prompt(monkeypatch, code=_SMALL_CHANGE_CODE, previous_code=None)

    assert "(unified diff against the previous round — see Instructions)" in diff_prompt
    assert "--- previous_round" in diff_prompt
    assert "+++ current_round" in diff_prompt
    assert _SMALL_CHANGE_CODE not in diff_prompt
    assert len(diff_prompt) < len(full_prompt)


def test_near_total_rewrite_falls_back_to_full_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """A near-total-rewrite still falls back to full text, per
    ``diff_or_full``'s existing size-comparison rule — confirms this call
    site actually plumbs the fallback, not just the utility's own tests."""
    prompt = _run_and_capture_prompt(
        monkeypatch, code=_REWRITTEN_CODE, previous_code=_ORIGINAL_CODE
    )

    assert _REWRITTEN_CODE in prompt
    assert "(unified diff against the previous round — see Instructions)" not in prompt


def test_response_instructions_always_require_full_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regardless of diff vs. full input, the LLM must be told to always
    emit the complete fixed file, never a diff or partial patch."""
    prompt = _run_and_capture_prompt(
        monkeypatch, code=_SMALL_CHANGE_CODE, previous_code=_ORIGINAL_CODE
    )

    assert "must always be the complete fixed file" in prompt
