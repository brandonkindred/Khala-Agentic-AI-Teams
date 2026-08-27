"""Coverage for the DESIGN_REVIEW -> CODE_SYNTHESIS boundary invariant in
``StrategyLabOrchestrator._orchestrate_design_and_review``.

The invariant check was previously a bare ``assert``, silently disabled
under ``python -O``. It is now an explicit ``if``/``raise`` guard
(``OrchestratorContractError``) that must always fire, regardless of
interpreter optimization flags.

Preconditions:
    None — this module defines one self-contained test and imports its
    fixtures from ``investment_team.tests.conftest``.
Postconditions:
    Importing this module registers
    ``test_orchestrate_design_and_review_raises_when_ready_flips_false``.

Note on approach: unlike sibling tests (e.g.
``test_strategy_lab_phase_transitions.py``), this test does not drive
``_run_design_loop`` through stubbed ``design_agent``/``design_review_agent``
collaborators (see ``conftest.stub_design_loop``) — it can't. The guard
under test only fires if ``design_outcome.ready`` differs across two reads
of the *same* returned object, and ``_DesignLoopOutcome.ready`` is a plain
dataclass field that a real design-loop run never mutates in place; no
real ``_run_design_loop`` execution can produce that inconsistency. The
guard is therefore defensive against a future bug in ``_run_design_loop``
itself, not anything reachable via today's code, and this test
intentionally patches ``_run_design_loop`` and ``_DesignLoopOutcome.ready``
directly to exercise it.
"""

from __future__ import annotations

from unittest.mock import PropertyMock, patch

import pytest

from investment_team.strategy_lab.exceptions import OrchestratorContractError
from investment_team.strategy_lab.orchestrator import (
    StrategyLabOrchestrator,
    _DesignLoopOutcome,
    _DriftCollector,
)
from investment_team.tests.conftest import default_backtest_config, default_rsi_spec_dict


def test_orchestrate_design_and_review_raises_when_ready_flips_false() -> None:
    """If ``design_outcome.ready`` is no longer true by the time the
    boundary guard runs, the guard must raise ``OrchestratorContractError``
    — not silently proceed to emit the phase transition and return a
    ``record=None`` result, which is what a stripped-under--O bare
    ``assert`` would have allowed.

    Preconditions:
        None beyond a fresh ``StrategyLabOrchestrator``.
    Postconditions:
        ``_orchestrate_design_and_review`` raises
        ``OrchestratorContractError`` with a message containing "boundary
        invariant violated"; no ``_DesignPhaseResult`` is returned.
    """
    orch = StrategyLabOrchestrator()
    spec = orch._build_spec_from_dict(default_rsi_spec_dict(), strategy_id="strat-boundary-test")
    outcome = _DesignLoopOutcome(
        spec=spec,
        rationale="scripted rationale",
        ready=False,  # inert: `.ready` is patched at the class level below
        rounds=1,
        critique_history=[],
    )
    orch._run_design_loop = lambda **_kw: outcome

    # `_orchestrate_design_and_review` reads `design_outcome.ready` exactly
    # twice: once at the short-circuit check, once at the boundary guard.
    # Patching `ready` as a class-level PropertyMock lets the first read
    # return True (bypassing the short-circuit) and the second return
    # False (tripping the guard) on the *same* `outcome` instance — a
    # transition no real, un-mutated `_DesignLoopOutcome` could produce.
    with patch.object(
        _DesignLoopOutcome,
        "ready",
        new_callable=PropertyMock,
        side_effect=[True, False],
        create=True,
    ):
        with pytest.raises(OrchestratorContractError, match="boundary invariant violated"):
            orch._orchestrate_design_and_review(
                prior_records=[],
                signal_briefs=None,
                directives=[],
                exclude_asset_classes=None,
                config=default_backtest_config(),
                all_gate_results=[],
                emit=lambda *a, **k: None,
                design_attempt=0,
                phase_back_count=0,
                drift_collector=_DriftCollector(),
            )
