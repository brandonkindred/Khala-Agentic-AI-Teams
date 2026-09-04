"""Unit tests for ``shared.graph.progress.GraphProgressReporter``.

The reporter is a pure adapter over a caller-supplied ``job_updater``
callable, so these tests drive it with a recording stub — no job service,
job store, or Strands graph is involved.
"""

from __future__ import annotations

import pytest

from shared.graph.progress import GraphProgressReporter


class _RecordingUpdater:
    """Callable stub capturing every ``(phase, detail, pct)`` it receives."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float]] = []

    def __call__(self, phase: str, detail: str, pct: float) -> None:
        self.calls.append((phase, detail, pct))


def _raising_updater(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("job store unreachable")


def test_on_node_start_reports_pre_completion_percentage() -> None:
    updater = _RecordingUpdater()
    reporter = GraphProgressReporter(updater, total_nodes=4, base_phase="SOC2 Audit")

    reporter.on_node_start("collect")

    assert updater.calls == [("SOC2 Audit", "Running collect...", 0.0)]


def test_on_node_complete_advances_percentage() -> None:
    updater = _RecordingUpdater()
    reporter = GraphProgressReporter(updater, total_nodes=4)

    reporter.on_node_complete("collect")
    reporter.on_node_complete("assess")

    assert updater.calls == [
        ("Graph Execution", "Completed collect", 0.25),
        ("Graph Execution", "Completed assess", 0.5),
    ]


def test_on_node_start_reflects_prior_completions() -> None:
    updater = _RecordingUpdater()
    reporter = GraphProgressReporter(updater, total_nodes=2)

    reporter.on_node_complete("first")
    reporter.on_node_start("second")

    assert updater.calls[-1] == ("Graph Execution", "Running second...", 0.5)


def test_on_done_reports_full_progress() -> None:
    updater = _RecordingUpdater()
    reporter = GraphProgressReporter(updater, total_nodes=3, base_phase="Pipeline")

    reporter.on_done()

    assert updater.calls == [("Pipeline", "Complete", 1.0)]


@pytest.mark.parametrize("total_nodes", [0, -5])
def test_non_positive_total_nodes_cannot_divide_by_zero(total_nodes: int) -> None:
    """``max(total_nodes, 1)`` keeps the percentage arithmetic defined."""
    updater = _RecordingUpdater()
    reporter = GraphProgressReporter(updater, total_nodes=total_nodes)

    reporter.on_node_complete("only")

    assert updater.calls == [("Graph Execution", "Completed only", 1.0)]


def test_updater_failures_are_swallowed_on_every_hook() -> None:
    """Progress reporting is best-effort: a broken updater never breaks the graph."""
    reporter = GraphProgressReporter(_raising_updater, total_nodes=2)

    reporter.on_node_start("a")
    reporter.on_node_complete("a")
    reporter.on_done()


def test_completion_count_advances_even_when_updater_raises() -> None:
    """The counter is bumped before the guarded call, so failures don't stall it."""
    calls: list[float] = []

    def flaky(_phase: str, _detail: str, pct: float) -> None:
        calls.append(pct)
        raise RuntimeError("boom")

    reporter = GraphProgressReporter(flaky, total_nodes=2)
    reporter.on_node_complete("a")
    reporter.on_node_complete("b")

    assert calls == [0.5, 1.0]
