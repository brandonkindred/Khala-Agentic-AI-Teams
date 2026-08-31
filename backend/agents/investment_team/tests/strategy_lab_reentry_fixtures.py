"""Shared fixture forcing ``SpecImplementabilityError`` after a known point of
partial convergence, driven identically by a thread-mode and a Temporal-mode
test.

Single source of truth for the "known point of partial convergence" (how many
design-review rounds complete before the forced synthesis-boundary failure)
and for the forced failure itself, so
``test_strategy_lab_checkpoint_crash_resumption.py``'s thread-mode re-entry
tests and its Temporal-mode re-entry test can't drift to different round
counts or differently-shaped errors -- a prerequisite for a later cross-mode
parity test to compare apples to apples.

The two modes' *injection mechanisms* stay separate (thread-mode drives a
real in-process design/review round loop; Temporal-mode fakes a higher-level
seam through a real Worker/WorkflowEnvironment) -- they cannot be literally
unified. What is shared is what both fixtures agree the "known point" and the
forced error must be.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List

if TYPE_CHECKING:
    import pytest

    from investment_team.strategy_lab.exceptions import SpecImplementabilityError

# The single source of truth for "a known number of refinement/alignment
# rounds have completed" -- both the thread-mode and Temporal-mode re-entry
# tests derive their round count and per-attempt LLM-call cost from this
# constant instead of each hardcoding its own value.
REENTRY_REVIEW_NOT_READY_ROUNDS: int = 1


def design_attempt_llm_call_cost(review_not_ready_rounds: int) -> int:
    """One design attempt's known LLM-call cost for ``review_not_ready_rounds``
    completed not-ready review rounds: one ``design_agent.run()`` + one
    ``design_review_agent.run()`` per round (``review_not_ready_rounds``
    not-ready rounds plus the final ready round) + one ``design_agent.revise()``
    per not-ready round.

    Preconditions:
        ``review_not_ready_rounds >= 0``.
    Postconditions:
        Returns the exact number of LLM-budget units one design attempt
        charges before converging, given that round count.
    """
    return 1 + (review_not_ready_rounds + 1) + review_not_ready_rounds


def synthesis_boundary_spec_implementability_error(
    *, spec: Any, spec_implicated: bool
) -> "SpecImplementabilityError":
    """Build the ``SpecImplementabilityError`` both the thread-mode and
    Temporal-mode re-entry tests raise at the synthesis boundary after
    partial convergence, varying only ``spec_implicated`` (``False`` --
    resumable; ``True`` -- forces a full restart).

    Preconditions:
        ``spec`` is the ``StrategySpec`` the forced failure should carry as
        ``last_spec``.
    Postconditions:
        Returns (does not raise) a ``SpecImplementabilityError`` with
        ``failure_phase="synthesis"``, ``last_code=""``, and the given
        ``spec_implicated``, ready for the caller to ``raise``.
    """
    from investment_team.strategy_lab.exceptions import SpecImplementabilityError

    evidence = (
        "forced fail at synthesis boundary, spec-implicated"
        if spec_implicated
        else "forced fail at synthesis boundary, not spec-implicated"
    )
    return SpecImplementabilityError(
        evidence,
        failure_phase="synthesis",
        last_spec=spec,
        last_code="",
        spec_implicated=spec_implicated,
    )


def _charging_design_stubs(
    monkeypatch: "pytest.MonkeyPatch", orch: Any, *, review_not_ready_rounds: int
) -> int:
    """Wire ``design_agent.run``/``revise`` and ``design_review_agent.run`` so
    each simulated call charges one unit of the active LLM budget, and the
    reviewer returns ``ready=False`` for ``review_not_ready_rounds`` rounds
    (a known number of design-refinement rounds) before converging on each
    design attempt -- so a fully re-run attempt pays the identical, known
    cost every time (the review-round counter resets on every ``run()``,
    i.e. at the start of each design attempt, mirroring one real design
    attempt's shape rather than accumulating across attempts).

    Postconditions:
        Returns the exact number of budget units one design attempt charges
        (``design_attempt_llm_call_cost(review_not_ready_rounds)``).
    """
    from investment_team.strategy_lab.agents._llm_budget import charge_active_budget
    from investment_team.strategy_lab.agents.design_review import SpecCritique

    from .test_strategy_lab_phase_transitions import _spec_dict

    def _revised_spec_dict() -> Any:
        revised = _spec_dict()
        revised["hypothesis"] = "revised hypothesis"
        return revised

    review_calls = {"n": 0}

    def _run(**_kw: Any) -> Any:
        review_calls["n"] = 0
        charge_active_budget()
        return _spec_dict(), "scripted rationale"

    def _review(*_a: Any, **_kw: Any) -> SpecCritique:
        charge_active_budget()
        review_calls["n"] += 1
        if review_calls["n"] <= review_not_ready_rounds:
            return SpecCritique(ready=False, rationale="tighten entry threshold")
        return SpecCritique(ready=True, rationale="ok")

    def _revise(*_a: Any, **_kw: Any) -> Any:
        charge_active_budget()
        return _revised_spec_dict(), "revised rationale"

    monkeypatch.setattr(orch.design_agent, "run", _run)
    monkeypatch.setattr(orch.design_review_agent, "run", _review)
    monkeypatch.setattr(orch.design_agent, "revise", _revise)

    return design_attempt_llm_call_cost(review_not_ready_rounds)


def _capture_cycle_budget(monkeypatch: "pytest.MonkeyPatch") -> List[Any]:
    """Monkeypatch ``orchestrator_module.LLMCallBudget`` to a wrapper that
    still constructs the real class but stashes every instance ``run_cycle``
    creates, so the test can read ``.calls_made`` after the call returns.
    ``run_cycle`` never exposes the budget on its return value in thread
    mode (that field is a Temporal-activity-output concept, from
    ``run_design_attempt_activity``'s ``out["budget_calls"]``) -- one
    instance is created per cycle (``orchestrator.py``, bound for the whole
    attempt loop via ``use_budget``), so ``captured[0]`` is the whole
    cycle's real cost.
    """
    from investment_team.strategy_lab import orchestrator as orchestrator_module

    captured: List[Any] = []
    real_cls = orchestrator_module.LLMCallBudget

    def _capturing(*args: Any, **kwargs: Any) -> Any:
        budget = real_cls(*args, **kwargs)
        captured.append(budget)
        return budget

    monkeypatch.setattr(orchestrator_module, "LLMCallBudget", _capturing)
    return captured
