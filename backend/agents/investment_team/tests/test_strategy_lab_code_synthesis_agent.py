"""Contract tests for :class:`CodeSynthesisAgent`.

Properties under test:
* Returns Python code (non-empty string).
* The spec passed in is not mutated by the call.
* Markdown ``` ```python ... ``` ``` fencing is stripped.
* LLM transport faults raise :class:`CodeSynthesisError`.
* Empty / whitespace-only responses raise :class:`CodeSynthesisError`.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from investment_team.models import StrategySpec
from investment_team.strategy_lab.agents.code_synthesis import (
    CodeSynthesisAgent,
    CodeSynthesisError,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _ReturningAgent:
    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.calls: List[str] = []

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self._payload


class _RaisingAgent:
    def __call__(self, _prompt: str) -> str:
        raise RuntimeError("transport down")


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-synth-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="RSI mean reversion",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op="<",
                    rhs=30,
                ),
            )
        ],
        exit_rules=[
            SignalExitRule(
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op=">",
                    rhs=70,
                )
            )
        ],
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
    )


def _patch_synthesis(
    monkeypatch: pytest.MonkeyPatch, payload: str | None = None, *, raise_: bool = False
) -> Any:
    agent: Any = _RaisingAgent() if raise_ else _ReturningAgent(payload or "")
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.code_synthesis.Agent",
        lambda **_kwargs: agent,
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.code_synthesis.get_strands_model",
        lambda role: object(),
    )
    return agent


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_returns_code_for_valid_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    code = "from contract import Strategy\n\nclass S(Strategy):\n    pass\n"
    _patch_synthesis(monkeypatch, code)

    out = CodeSynthesisAgent().run(_spec())

    assert out == code.strip()
    assert "Strategy" in out


def test_strips_markdown_python_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    fenced = "```python\nfrom contract import Strategy\n\nclass S(Strategy):\n    pass\n```"
    _patch_synthesis(monkeypatch, fenced)

    out = CodeSynthesisAgent().run(_spec())

    assert not out.startswith("```")
    assert "Strategy" in out


def test_spec_is_not_mutated_by_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_synthesis(monkeypatch, "# code\n")

    spec = _spec()
    snapshot = spec.model_dump()
    CodeSynthesisAgent().run(spec)

    assert spec.model_dump() == snapshot


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_transport_failure_raises_code_synthesis_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_synthesis(monkeypatch, raise_=True)

    with pytest.raises(CodeSynthesisError) as exc:
        CodeSynthesisAgent().run(_spec())

    assert "RuntimeError" in str(exc.value)


def test_empty_response_raises_code_synthesis_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_synthesis(monkeypatch, "   \n  ")

    with pytest.raises(CodeSynthesisError):
        CodeSynthesisAgent().run(_spec())


def test_fence_only_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A response that's literally an empty fence collapses to no code."""
    _patch_synthesis(monkeypatch, "```python\n\n```")

    with pytest.raises(CodeSynthesisError):
        CodeSynthesisAgent().run(_spec())
