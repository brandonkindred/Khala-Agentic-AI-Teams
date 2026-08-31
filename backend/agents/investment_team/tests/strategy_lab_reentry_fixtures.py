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
``test_strategy_lab_cross_attempt_resume.py``'s re-entry tests also consume
this module (via ``_review_loop_stubs``, which defaults to
``REENTRY_REVIEW_NOT_READY_ROUNDS``).

The two modes' *injection mechanisms* stay separate (thread-mode drives a
real in-process design/review round loop; Temporal-mode fakes a higher-level
seam through a real Worker/WorkflowEnvironment) -- they cannot be literally
unified. What is shared is what both fixtures agree the "known point" and the
forced error must be.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List

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
    if review_not_ready_rounds < 0:
        raise ValueError("review_not_ready_rounds must be >= 0")
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


def _not_ready_then_ready_critiques(review_not_ready_rounds: int) -> List[Any]:
    """Build the ``review_not_ready_rounds`` not-ready critiques followed by
    the final ready critique that both re-entry fixture shapes below drive
    a design attempt's review loop through.

    Preconditions:
        ``review_not_ready_rounds >= 0``.
    Postconditions:
        Returns ``review_not_ready_rounds + 1`` critiques -- the first
        ``review_not_ready_rounds`` with ``ready=False``, the last with
        ``ready=True``.
    """
    if review_not_ready_rounds < 0:
        raise ValueError("review_not_ready_rounds must be >= 0")

    from investment_team.strategy_lab.agents.design_review import SpecCritique

    return [
        SpecCritique(ready=False, rationale="tighten entry threshold")
        for _ in range(review_not_ready_rounds)
    ] + [SpecCritique(ready=True, rationale="ok")]


def _revised_spec_dict(spec_dict: Any) -> Any:
    """Return a copy of ``spec_dict`` with a marker ``hypothesis`` revision
    applied (what ``design_agent.revise`` is stubbed to produce)."""
    revised = dict(spec_dict)
    revised["hypothesis"] = "revised hypothesis"
    return revised


def _revising_stub(review_not_ready_rounds: int) -> Any:
    """Build the ``design_revising`` stub both re-entry fixture shapes wire
    onto ``design_agent.revise``: one marker revision per not-ready round.

    Preconditions:
        ``review_not_ready_rounds >= 0``.
    Postconditions:
        Returns a callable yielding ``review_not_ready_rounds`` successive
        revised spec dicts, then raising ``StopIteration`` if called again.
    """
    from .conftest import default_rsi_spec_dict, design_revising

    return design_revising(
        [_revised_spec_dict(default_rsi_spec_dict()) for _ in range(review_not_ready_rounds)]
    )


def _charging_design_stubs(
    monkeypatch: "pytest.MonkeyPatch", orch: Any, *, review_not_ready_rounds: int
) -> int:
    """Wire ``design_agent.run``/``revise`` and ``design_review_agent.run`` so
    each simulated call charges one unit of the active LLM budget, and the
    reviewer returns ``ready=False`` for ``review_not_ready_rounds`` rounds
    (a known number of design-refinement rounds) before converging on each
    design attempt -- so a fully re-run attempt pays the identical, known
    cost every time (the review/revise stubs are rebuilt fresh on every
    ``run()``, i.e. at the start of each design attempt, mirroring one real
    design attempt's shape rather than accumulating across attempts).

    Built on top of ``conftest.py``'s ``design_returning``/``design_revising``/
    ``review_returning`` stub builders rather than re-implementing their
    return shapes, with only the per-call budget charge and the per-attempt
    rebuild genuinely new here.

    Postconditions:
        Returns the exact number of budget units one design attempt charges
        (``design_attempt_llm_call_cost(review_not_ready_rounds)``).
    """
    from investment_team.strategy_lab.agents._llm_budget import charge_active_budget

    from .conftest import default_rsi_spec_dict, design_returning, review_returning

    attempt: Dict[str, Any] = {}

    def _run(**kwargs: Any) -> Any:
        charge_active_budget()
        attempt["review"] = review_returning(
            *_not_ready_then_ready_critiques(review_not_ready_rounds)
        )
        attempt["revise"] = _revising_stub(review_not_ready_rounds)
        return design_returning(default_rsi_spec_dict())(**kwargs)

    def _review(*args: Any, **kwargs: Any) -> Any:
        charge_active_budget()
        return attempt["review"](*args, **kwargs)

    # design_agent.revise is called with a positional spec argument in
    # production code, but conftest.design_revising's stub only accepts
    # **kwargs and ignores its input regardless -- *args is accepted here
    # (matching the real call signature) and intentionally dropped before
    # delegating, not forwarded.
    def _revise(*args: Any, **kwargs: Any) -> Any:
        charge_active_budget()
        return attempt["revise"](**kwargs)

    monkeypatch.setattr(orch.design_agent, "run", _run)
    monkeypatch.setattr(orch.design_review_agent, "run", _review)
    monkeypatch.setattr(orch.design_agent, "revise", _revise)

    return design_attempt_llm_call_cost(review_not_ready_rounds)


def _review_loop_stubs(
    monkeypatch: "pytest.MonkeyPatch",
    orch: Any,
    *,
    review_not_ready_rounds: int = REENTRY_REVIEW_NOT_READY_ROUNDS,
) -> None:
    """Wire ``design_review_agent.run``/``design_agent.revise`` (but not
    ``design_agent.run`` -- callers already have that stubbed, typically via
    ``_stub_pipeline_for_happy_path``) so the review loop returns
    ``ready=False`` for ``review_not_ready_rounds`` rounds before converging,
    without charging any LLM budget. Shared by
    ``test_strategy_lab_cross_attempt_resume.py``'s several re-entry tests,
    which only ever drive one such sequence across a whole cycle (no
    per-attempt reset needed, unlike ``_charging_design_stubs`` above).

    Preconditions:
        ``orch.design_agent.run`` is already stubbed by the caller.
    Postconditions:
        ``orch.design_review_agent.run`` and ``orch.design_agent.revise``
        are replaced with stubs producing the known not-ready-then-ready
        sequence.
    """
    from .conftest import review_returning

    revise_stub = _revising_stub(review_not_ready_rounds)

    # See the identical comment in _charging_design_stubs above: *args is
    # accepted to match design_agent.revise's real positional call
    # signature, then intentionally dropped -- conftest.design_revising's
    # stub only accepts **kwargs and ignores its input regardless.
    def _revise(*args: Any, **kwargs: Any) -> Any:
        return revise_stub(**kwargs)

    monkeypatch.setattr(
        orch.design_review_agent,
        "run",
        review_returning(*_not_ready_then_ready_critiques(review_not_ready_rounds)),
    )
    monkeypatch.setattr(orch.design_agent, "revise", _revise)


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
