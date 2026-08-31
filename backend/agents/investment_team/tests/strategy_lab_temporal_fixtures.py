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
        # Matches ``SpecImplementabilityError.spec_implicated``'s own default
        # (``exceptions.py``) — every current production raise site passes
        # this explicitly as ``True``, so the default here keeps every
        # existing test's full-restart behavior unchanged unless a test
        # deliberately overrides it to exercise cross-attempt resume.
        "spec_implicated": True,
        "convergence_tracker_state": {"trial_count": 0},
        "gate_results": [],
        "budget_calls": 0,
        "drift": {"spec_history": [], "code_history": [], "gate_timeline": []},
        # Matches the real activity's always-present field
        # (``_pipeline_checkpoints_to_wire()`` in ``activities.py``) — a list
        # of ``PipelineCheckpoint.model_dump(mode="json")`` dicts. Empty by
        # default (no checkpoint captured for this attempt).
        "pipeline_checkpoints": [],
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
        "pipeline_checkpoints": [],
    }
    base.update(overrides)
    return base


def checkpoint_json(
    checkpoint_cls, *, run_id: str = "run-1", generation: int = 1, **overrides: Any
) -> Dict[str, Any]:
    """Build one real ``PipelineCheckpoint`` subclass instance -- with the
    identity fields matching the ``run_id``/``generation`` convention used
    across both ``test_strategy_lab_temporal_workflows.py`` and
    ``test_strategy_lab_temporal_workflow_replay.py`` -- and return its wire
    (``model_dump(mode="json")``) form, exactly as ``activities.py``'s
    ``_pipeline_checkpoints_to_wire`` produces it. Building from the real
    Pydantic classes (rather than hand-rolled dicts) means this fixture can't
    drift from the real wire shape. ``cycle_scope`` (default
    ``"cycle-scope-1"``) and every stage-specific field are overridable via
    ``**overrides`` like any other key.
    """
    from investment_team.models import StrategySpec
    from investment_team.strategy_lab import phases

    spec = overrides.pop(
        "spec",
        StrategySpec(
            strategy_id="strat-1",
            authored_by="DesignAgent",
            asset_class="stocks",
            hypothesis="test hypothesis",
            signal_definition="test signal",
            timeframe="1d",
        ),
    )
    code = overrides.pop("code", "def run(): pass")
    base: Dict[str, Any] = {
        "run_id": run_id,
        "cycle_scope": "cycle-scope-1",
        "design_attempt": 0,
        "generation": generation,
        "spec_hash": phases.hash_spec(spec),
        "code_hash": phases.hash_code(code)
        if "code" in checkpoint_cls.model_fields
        else phases.hash_code(None),
        "captured_at": "2026-08-27T00:00:00Z",
        "budget_calls": 5,
        "gate_results": [],
        "spec": spec,
        "rationale": "because",
    }
    if "code" in checkpoint_cls.model_fields:
        base["code"] = code
    base.update(overrides)
    return checkpoint_cls(**base).model_dump(mode="json")
