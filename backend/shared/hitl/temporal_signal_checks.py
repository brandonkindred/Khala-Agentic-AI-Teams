"""Shared test-assertion helper for verifying a workflow class registers the
``submit_answers`` signal from :mod:`shared.hitl.temporal_signal`.

Extracted so ``planning_team/tests/test_temporal_workflow_signal.py`` and
``software_engineering_team/tests/test_shared_infra_gap_coverage.py`` -- which
both need this exact assertion (see each file's module docstring for why the
check is duplicated across two CI-collection boundaries) -- share one
implementation instead of two hand-maintained copies that could silently
drift on a temporalio upgrade.
"""

from __future__ import annotations

from typing import Type

from temporalio import workflow

from shared.hitl.temporal_signal import SUBMIT_ANSWERS_SIGNAL


def assert_workflow_registers_submit_answers(workflow_cls: Type) -> None:
    """Assert ``workflow_cls`` is a ``@workflow.defn`` class registering the
    ``submit_answers`` signal under the #7451-specified wire-contract name.

    Preconditions:
        - ``workflow_cls`` is a class, decorated or not with ``@workflow.defn``.
    Postconditions:
        - Raises ``AssertionError`` if ``workflow_cls`` lacks ``@workflow.defn``,
          or if the signal registered under :data:`SUBMIT_ANSWERS_SIGNAL` is
          missing or not literally named ``"submit_answers"`` (pinning the
          literal, not just the constant, so a changed constant value would
          fail this assertion instead of silently passing).

    Uses ``temporalio.workflow._Definition``, a private introspection API --
    the only practical way to assert signal registration without standing up
    a worker. Re-verify this helper on temporalio upgrades.
    """
    defn = workflow._Definition.from_class(workflow_cls)
    assert defn is not None, f"{workflow_cls.__name__} is missing the @workflow.defn decorator"
    assert SUBMIT_ANSWERS_SIGNAL == "submit_answers"
    assert SUBMIT_ANSWERS_SIGNAL in defn.signals
    assert defn.signals[SUBMIT_ANSWERS_SIGNAL].name == "submit_answers"
