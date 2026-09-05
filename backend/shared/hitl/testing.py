"""Shared test-only assertion helper for verifying a workflow class registers
the ``submit_answers`` signal from :mod:`shared.hitl.temporal_signal`.

Not part of ``shared.hitl``'s production public API (see
``shared/hitl/__init__.py``, which deliberately does not re-export this or
``temporal_signal`` so ``temporalio`` stays out of the package's transitive
import graph for non-Temporal consumers) -- this module is imported only
from test files. Extracted so ``planning_team/tests/test_temporal_workflow_signal.py``
and ``software_engineering_team/tests/test_shared_infra_gap_coverage.py`` --
which both need this exact assertion (see each file's module docstring for
why the check is duplicated across two CI-collection boundaries) -- share one
implementation instead of two hand-maintained copies that could silently
drift on a temporalio upgrade.
"""

from __future__ import annotations

from typing import Any, Type

from temporalio import workflow

from shared.hitl.temporal_signal import SUBMIT_ANSWERS_SIGNAL


def get_workflow_definition(workflow_cls: Type) -> Any:
    """Return the temporalio ``_Definition`` for a ``@workflow.defn`` class,
    or ``None`` if it isn't one.

    Centralizes the private ``temporalio.workflow._Definition``/``from_class``
    touchpoint in one place -- callers that need definition-level details
    beyond what :func:`assert_workflow_registers_submit_answers` checks (e.g.
    ``defn.name``, ``defn.run_fn``) should go through this accessor rather
    than importing ``temporalio.workflow`` and calling the private API
    directly, so a temporalio upgrade only needs reconciling here.

    Preconditions:
        - ``workflow_cls`` is a class, decorated or not with ``@workflow.defn``.
    Postconditions:
        - Returns the class's ``_Definition`` if decorated, else ``None``.
          Never raises for a well-formed class argument.
    """
    return workflow._Definition.from_class(workflow_cls)


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
          fail this assertion instead of silently passing). The
          missing-signal case's message names ``"submit_answers"``
          explicitly, so a caller asserting on the failure message can
          distinguish it from an unrelated assertion failure inside this
          helper.

    Uses ``temporalio.workflow._Definition`` via :func:`get_workflow_definition`,
    a private introspection API -- the only practical way to assert signal
    registration without standing up a worker. Re-verify this helper on
    temporalio upgrades.
    """
    defn = get_workflow_definition(workflow_cls)
    assert defn is not None, f"{workflow_cls.__name__} is missing the @workflow.defn decorator"
    assert SUBMIT_ANSWERS_SIGNAL == "submit_answers"
    assert SUBMIT_ANSWERS_SIGNAL in defn.signals, (
        f"{workflow_cls.__name__} does not register the 'submit_answers' signal"
    )
    assert defn.signals[SUBMIT_ANSWERS_SIGNAL].name == "submit_answers"
