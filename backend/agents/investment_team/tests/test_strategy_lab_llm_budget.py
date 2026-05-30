"""Unit tests for the design-phase LLM-call budget primitive."""

from __future__ import annotations

import pytest

from investment_team.strategy_lab.agents._llm_budget import (
    DesignBudgetExhausted,
    LLMCallBudget,
)


def test_charge_admits_exactly_limit_calls() -> None:
    """A budget of N allows exactly N charges; the (N+1)th raises."""
    budget = LLMCallBudget(3)

    budget.charge()
    budget.charge()
    budget.charge()
    assert budget.calls_made == 3

    with pytest.raises(DesignBudgetExhausted) as exc_info:
        budget.charge()

    # The raise does not increment past the limit.
    assert budget.calls_made == 3
    assert exc_info.value.limit == 3
    assert exc_info.value.calls_made == 3


def test_limit_of_one_allows_single_call() -> None:
    budget = LLMCallBudget(1)
    budget.charge()
    with pytest.raises(DesignBudgetExhausted):
        budget.charge()


def test_exhausted_message_names_env_var() -> None:
    budget = LLMCallBudget(1)
    budget.charge()
    with pytest.raises(DesignBudgetExhausted) as exc_info:
        budget.charge()
    assert "STRATEGY_LAB_DESIGN_MAX_LLM_CALLS" in str(exc_info.value)


def test_limit_below_one_is_rejected() -> None:
    """The constructor enforces the ``limit >= 1`` precondition."""
    with pytest.raises(AssertionError):
        LLMCallBudget(0)
