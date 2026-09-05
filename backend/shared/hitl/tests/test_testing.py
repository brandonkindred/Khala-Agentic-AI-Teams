"""Unit tests for shared.hitl.testing -- the shared
``assert_workflow_registers_submit_answers`` helper both
``planning_team/tests/test_temporal_workflow_signal.py`` and
``software_engineering_team/tests/test_shared_infra_gap_coverage.py`` call.
"""

from __future__ import annotations

import pytest
from temporalio import workflow

from shared.hitl.temporal_signal import HitlAnswerSignalMixin
from shared.hitl.testing import assert_workflow_registers_submit_answers


@workflow.defn(name="_ChecksTestWorkflow")
class _WorkflowWithSignal(HitlAnswerSignalMixin):
    """Module-level (not local) because temporalio's @workflow.run rejects
    classes defined inside a function."""

    @workflow.run
    async def run(self) -> None:
        return None


@workflow.defn(name="_ChecksTestWorkflowNoSignal")
class _WorkflowWithoutSignal:
    """Module-level for the same reason -- never mixes in
    HitlAnswerSignalMixin, so it registers no submit_answers signal."""

    @workflow.run
    async def run(self) -> None:
        return None


@workflow.defn(name="_ChecksTestWorkflowDifferentSignal")
class _WorkflowWithDifferentSignal:
    """Module-level for the same reason -- registers a signal, but not the
    one the helper is checking for, to prove the helper looks for
    submit_answers specifically rather than accepting any signal at all."""

    @workflow.run
    async def run(self) -> None:
        return None

    @workflow.signal
    async def some_other_signal(self, payload: dict) -> None:
        return None


def test_assert_helper_accepts_a_workflow_registering_the_signal() -> None:
    """Positive path, exercised implicitly by every caller of this helper --
    pinned directly here too so this file documents the full contract."""
    assert_workflow_registers_submit_answers(_WorkflowWithSignal)


def test_assert_helper_rejects_undecorated_class() -> None:
    """A class with no @workflow.defn has no temporalio _Definition at all --
    the helper's first assertion must catch this before touching .signals."""

    class NotADefn:
        pass

    with pytest.raises(AssertionError, match="missing the @workflow.defn decorator"):
        assert_workflow_registers_submit_answers(NotADefn)


def test_assert_helper_rejects_workflow_without_the_signal() -> None:
    """A @workflow.defn class that never mixes in HitlAnswerSignalMixin (or
    otherwise registers submit_answers) must fail the helper's signal check,
    not silently pass because the class itself is well-formed."""
    with pytest.raises(AssertionError, match="does not register the 'submit_answers' signal"):
        assert_workflow_registers_submit_answers(_WorkflowWithoutSignal)


def test_assert_helper_rejects_workflow_with_only_a_different_signal() -> None:
    """A @workflow.defn class registering some other signal must still fail --
    proves the helper checks for submit_answers by name, not just that the
    class registers at least one signal of any name."""
    with pytest.raises(AssertionError, match="does not register the 'submit_answers' signal"):
        assert_workflow_registers_submit_answers(_WorkflowWithDifferentSignal)
