"""Unit tests for the design-phase LLM-call budget primitive."""

from __future__ import annotations

import pytest

from investment_team.strategy_lab.agents._llm_budget import (
    DesignBudgetExhausted,
    LLMCallBudget,
    _annotate_budget_exhaustion,
    active_budget,
    charge_active_budget,
    use_budget,
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


def test_charge_active_budget_is_noop_without_binding() -> None:
    """Outside ``use_budget`` there is no active budget, so charging is a
    no-op (agents invoked outside a design cycle are unaffected)."""
    assert active_budget() is None
    charge_active_budget()  # must not raise
    assert active_budget() is None


def test_use_budget_binds_and_charges_then_restores() -> None:
    """``use_budget`` binds the budget for the block; ``charge_active_budget``
    charges it; the prior (None) binding is restored on exit."""
    budget = LLMCallBudget(5)
    with use_budget(budget):
        assert active_budget() is budget
        charge_active_budget()
        charge_active_budget()
        assert budget.calls_made == 2
    assert active_budget() is None


def test_use_budget_restores_binding_even_on_exception() -> None:
    """The binding is reset even if the block raises (e.g. on exhaustion)."""
    budget = LLMCallBudget(1)
    with pytest.raises(DesignBudgetExhausted):
        with use_budget(budget):
            charge_active_budget()  # ok
            charge_active_budget()  # trips
    assert active_budget() is None


def test_nested_use_budget_restores_outer() -> None:
    """Nested bindings restore the outer budget on inner exit."""
    outer = LLMCallBudget(3)
    inner = LLMCallBudget(3)
    with use_budget(outer):
        with use_budget(inner):
            assert active_budget() is inner
        assert active_budget() is outer


def _tripped() -> DesignBudgetExhausted:
    """Build a real budget trip so the annotate helper gets a genuine exc."""
    budget = LLMCallBudget(1)
    budget.charge()
    try:
        budget.charge()
    except DesignBudgetExhausted as exc:
        return exc
    raise AssertionError("budget did not trip")  # pragma: no cover


def test_annotate_returns_same_exc_and_sets_spec() -> None:
    """The helper mutates and returns the same exception; spec is stamped."""
    exc = _tripped()
    spec = object()

    result = _annotate_budget_exhaustion(exc, spec)

    assert result is exc
    assert exc.latest_spec is spec


def test_annotate_code_variant_sets_only_code() -> None:
    """The code variant stamps ``latest_code`` and leaves rationale/repair
    unset (the refinement/alignment/synthesis call sites)."""
    exc = _tripped()
    spec = object()
    code = "return 1"

    _annotate_budget_exhaustion(exc, spec, code=code)

    assert exc.latest_spec is spec
    assert exc.latest_code == code
    assert not hasattr(exc, "latest_rationale")
    assert not hasattr(exc, "mechanical_repair_count")


def test_annotate_rationale_variant_sets_rationale_and_repair_count() -> None:
    """The design-loop variant stamps rationale + mechanical-repair count and
    leaves ``latest_code`` unset."""
    exc = _tripped()
    spec = object()

    _annotate_budget_exhaustion(exc, spec, rationale="why", mechanical_repair_count=2)

    assert exc.latest_spec is spec
    assert exc.latest_rationale == "why"
    assert exc.mechanical_repair_count == 2
    assert not hasattr(exc, "latest_code")


def test_annotate_zero_repair_count_is_stamped() -> None:
    """A repair count of 0 is a real value (not ``None``) and must be set."""
    exc = _tripped()

    _annotate_budget_exhaustion(exc, object(), mechanical_repair_count=0)

    assert exc.mechanical_repair_count == 0


def test_annotate_omitted_kwargs_leave_attrs_absent() -> None:
    """Optional annotations default to unset when their kwarg is omitted."""
    exc = _tripped()

    _annotate_budget_exhaustion(exc, object())

    assert not hasattr(exc, "latest_code")
    assert not hasattr(exc, "latest_rationale")
    assert not hasattr(exc, "mechanical_repair_count")


def test_annotate_rejects_non_budget_exception() -> None:
    """Precondition: ``exc`` must be a ``DesignBudgetExhausted``."""
    with pytest.raises(AssertionError):
        _annotate_budget_exhaustion(ValueError("nope"), object())
