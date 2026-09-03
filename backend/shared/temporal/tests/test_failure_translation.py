"""Unit tests for ``shared.temporal.failure_translation``.

``translate_workflow_failure`` is a pure walk over the standard exception
chain, matching an ``ApplicationError``-shaped ``type`` marker. The stand-ins
below reproduce that shape (a ``type`` string plus an optional ``message``)
without needing a Temporal server or a real ``WorkflowFailureError``.
"""

from __future__ import annotations

import pytest

from shared.temporal.failure_translation import (
    DEFAULT_MAX_CAUSE_DEPTH,
    translate_workflow_failure,
)


class MarkedError(Exception):
    """Stand-in for ``temporalio.exceptions.ApplicationError``."""

    def __init__(self, marker: str, message: str | None = None) -> None:
        super().__init__(message or marker)
        self.type = marker
        self.message = message


class PlainError(Exception):
    """A chain node carrying no ``type`` marker at all."""


class JobNotFound(Exception):
    """Native domain exception the dispatch boundary already handles."""


class Conflict(Exception):
    """A second native exception, to prove the first match wins."""


MARKERS: dict[str, type[Exception]] = {"JobNotFound": JobNotFound, "Conflict": Conflict}


def _chain(*nodes: BaseException) -> BaseException:
    """Link *nodes* outermost-first via ``__cause__`` and return the head.

    Preconditions:
        * ``nodes`` is non-empty (the head is ``nodes[0]``).
    """
    for outer, inner in zip(nodes, nodes[1:]):
        outer.__cause__ = inner
    return nodes[0]


def test_marker_at_the_top_of_the_chain_is_translated() -> None:
    exc = MarkedError("JobNotFound", "job 42 is gone")

    with pytest.raises(JobNotFound, match="job 42 is gone") as caught:
        translate_workflow_failure(exc, MARKERS)

    assert caught.value.__cause__ is exc


def test_marker_nested_under_an_activity_error_is_translated() -> None:
    """Temporal wraps the activity failure, so the marker is not always the head."""
    marker = MarkedError("JobNotFound", "job 42 is gone")
    head = _chain(PlainError("workflow failed"), PlainError("activity failed"), marker)

    with pytest.raises(JobNotFound, match="job 42 is gone"):
        translate_workflow_failure(head, MARKERS)


def test_context_links_are_followed_when_no_cause_is_set() -> None:
    marker = MarkedError("Conflict", "already claimed")
    head = PlainError("workflow failed")
    head.__context__ = marker

    with pytest.raises(Conflict, match="already claimed"):
        translate_workflow_failure(head, MARKERS)


def test_an_unmatched_cause_chain_shadows_a_matching_context() -> None:
    """Per-node ``__cause__ or __context__`` means a set __cause__ wins outright.

    The walk never falls back to a node's own ``__context__`` once that node
    has a ``__cause__`` at all, matched or not — so a marker sitting only on
    ``__context__`` here is never reached, and the call returns normally.
    """
    head = PlainError("workflow failed")
    head.__cause__ = PlainError("activity failed")
    head.__context__ = MarkedError("Conflict", "already claimed")

    assert translate_workflow_failure(head, MARKERS) is None


def test_str_is_used_when_the_node_carries_no_message() -> None:
    """str(exc) must differ from the marker, or a node.type-based regression would pass too."""
    exc = MarkedError("JobNotFound")
    exc.args = ("job 42 vanished without a message",)

    with pytest.raises(JobNotFound, match="job 42 vanished without a message"):
        translate_workflow_failure(exc, MARKERS)


def test_the_first_matching_marker_in_the_chain_wins() -> None:
    head = _chain(MarkedError("Conflict", "outer"), MarkedError("JobNotFound", "inner"))

    with pytest.raises(Conflict, match="outer"):
        translate_workflow_failure(head, MARKERS)


def test_unmatched_marker_returns_normally() -> None:
    """No match means the caller re-raises the original failure itself."""
    head = _chain(PlainError("workflow failed"), MarkedError("SomethingElse", "nope"))

    assert translate_workflow_failure(head, MARKERS) is None


def test_non_string_type_attribute_is_ignored() -> None:
    node = PlainError("odd")
    node.type = object()

    assert translate_workflow_failure(node, MARKERS) is None


def test_empty_marker_mapping_never_translates() -> None:
    assert translate_workflow_failure(MarkedError("JobNotFound", "x"), {}) is None


def test_a_cyclic_chain_terminates() -> None:
    first = PlainError("a")
    second = PlainError("b")
    first.__cause__ = second
    second.__cause__ = first

    assert translate_workflow_failure(first, MARKERS) is None


def test_a_marker_beyond_max_depth_is_not_reached() -> None:
    nodes: list[BaseException] = [PlainError(f"hop {i}") for i in range(DEFAULT_MAX_CAUSE_DEPTH)]
    nodes.append(MarkedError("JobNotFound", "too deep"))
    head = _chain(*nodes)

    assert translate_workflow_failure(head, MARKERS) is None


def test_max_depth_is_configurable() -> None:
    head = _chain(PlainError("a"), PlainError("b"), MarkedError("JobNotFound", "reachable"))

    assert translate_workflow_failure(head, MARKERS, max_depth=2) is None
    with pytest.raises(JobNotFound, match="reachable"):
        translate_workflow_failure(head, MARKERS, max_depth=3)
