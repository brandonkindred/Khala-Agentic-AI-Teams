"""Regression tests wiring ``shared.hitl.temporal_signal.HitlAnswerSignalMixin``
into ``PlanningWorkflow``.

Two layers, matching the pattern
``agent_team_studio/agentic_team_provisioning/tests/test_temporal_bootstrap.py``
uses for the same kind of check: a Temporal-definition-level assertion that the
``submit_answers`` signal is actually registered on the workflow class (a
dropped ``@workflow.signal``/base class would only otherwise surface when a
worker registers the workflow), plus a behavioral check driving the real
``PlanningWorkflow`` class directly (no Temporal server) to prove the mixin's
state machine is live on it, not just present in isolation.
"""

from __future__ import annotations

from temporalio import workflow

from planning_team.temporal.workflows import PlanningWorkflow
from shared.hitl.temporal_signal import SUBMIT_ANSWERS_SIGNAL


def test_planning_workflow_registers_submit_answers_signal() -> None:
    # Intentionally mirrored (same name) in
    # software_engineering_team/tests/test_shared_infra_gap_coverage.py, the
    # only CI-collected copy of this check -- keep both in sync if either
    # changes, especially the temporalio private API note below.
    # temporalio private API (_Definition); re-verify this test on temporalio upgrades.
    defn = workflow._Definition.from_class(PlanningWorkflow)
    assert defn is not None, "PlanningWorkflow is missing the @workflow.defn decorator"
    assert defn.name == "PlanningWorkflow"
    assert defn.run_fn.__name__ == "run"
    assert SUBMIT_ANSWERS_SIGNAL in defn.signals
    assert defn.signals[SUBMIT_ANSWERS_SIGNAL].name == "submit_answers"


def test_planning_workflow_submit_answers_accepts_matching_signal() -> None:
    """Drives the mixin's live state machine through the concrete PlanningWorkflow
    class (not just the standalone mixin) to prove the composition actually
    works, e.g. no ``__init__`` override on PlanningWorkflow shadows the
    mixin's state initialization."""
    wf = PlanningWorkflow()
    assert wf._active_resume_token is None
    assert wf._submitted_answers is None
    assert wf._buffered_signals == {}

    wf._active_resume_token = "tok-1"
    wf.submit_answers({"resume_token": "tok-1", "answers": [{"question_id": "q1"}]})

    assert wf._submitted_answers == [
        {"question_id": "q1", "selected_option_id": None, "other_text": None}
    ]
    assert wf._buffered_signals == {}


def test_planning_workflow_submit_answers_rejects_out_of_order_signal() -> None:
    """A signal for a pause that is not the one currently pending must not be
    applied to it, nor buffered for later -- a mismatched token while a pause
    is active has nothing to buffer against."""
    wf = PlanningWorkflow()
    wf._active_resume_token = "current-token"

    wf.submit_answers({"resume_token": "stale-token", "answers": [{"question_id": "q1"}]})

    assert wf._submitted_answers is None
    assert wf._buffered_signals == {}
