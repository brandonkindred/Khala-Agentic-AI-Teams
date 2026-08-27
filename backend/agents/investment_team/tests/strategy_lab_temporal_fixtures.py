"""Canonical wire-shape fixtures shared by ``StrategyLabCycleWorkflow`` tests.

Single source of truth for the cycle-config dict and the two
``run_design_attempt_activity``-shaped outcome dicts used both by
``test_strategy_lab_temporal_workflows.py`` (monkeypatched
``execute_activity``, no sandbox) and
``test_strategy_lab_temporal_workflow_replay.py`` (real embedded Temporal
server + ``Replayer``). Kept in one place so a change to either wire shape
can't drift the two test files out of sync with each other.
"""

from __future__ import annotations

from typing import Any, Dict


def config_dict(**overrides: Any) -> Dict[str, Any]:
    base = {"start_date": "2023-01-01", "end_date": "2023-12-31"}
    base.update(overrides)
    return base


WF_CONFIG = {
    "design_review_rounds": 20,
    "design_review_stall_rounds": 3,
    # Exercise the true default so a regression that resets the mechanical
    # repair flag (which hid two crash bugs in the earlier hand-ported port)
    # would surface here rather than being masked.
    "mechanical_repair_enabled": True,
    "code_conformance_retries": 2,
    "design_max_llm_calls": 120,
    "regime_summary_enabled": False,
    "max_design_reentries": 2,
}


def reentry_outcome(**overrides: Any) -> Dict[str, Any]:
    base = {
        "kind": "reentry",
        "evidence": "always fails",
        "last_spec": {"strategy_id": "strat-1"},
        "last_code": "code",
        "failure_phase": "evaluation",
        "design_context": {"rounds": 1, "critiques": [], "stop_reason": "x", "loop_telemetry": {}},
        "convergence_tracker_state": {"trial_count": 0},
        "gate_results": [],
        "budget_calls": 0,
        "drift": {"spec_history": [], "code_history": [], "gate_timeline": []},
    }
    base.update(overrides)
    return base


def record_outcome(**overrides: Any) -> Dict[str, Any]:
    base = {
        "kind": "record",
        "record": {"lab_record_id": "rec-1"},
        "convergence_tracker_state": {"trial_count": 1},
        "gate_results": [],
        "budget_calls": 3,
        "drift": {"spec_history": [], "code_history": [], "gate_timeline": []},
    }
    base.update(overrides)
    return base
