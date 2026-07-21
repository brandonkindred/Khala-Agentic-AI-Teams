"""CodingTeamSwarm revision-cap mixin: shared bump/compare/branch logic for the
per-task revision cap.

Extracted to remove duplication between ``swarm_review._ReviewMixin._return_for_revision``
and ``swarm_implementation._ImplementationMixin._handle_incomplete_implementation`` — pure
structural move, no behavior change. Composed onto ``CodingTeamSwarm`` in
``coding_team_orchestrator.py`` alongside the other mixins.

``MAX_TASK_REVISIONS`` is defined in ``coding_team_orchestrator.py`` and referenced via a
late-bound module reference (``_orch.MAX_TASK_REVISIONS``, resolved at call time) rather than
imported by name at module load time — see the equivalent note in ``swarm_review.py`` for why
(circular import at load time, and monkeypatchability of ``MAX_TASK_REVISIONS`` in tests).
"""

from __future__ import annotations

from typing import Callable, TypeVar

from software_engineering_team.models import Task

_T = TypeVar("_T")


class _RevisionCapMixin:
    """Shared bump/compare/branch logic for the per-task revision cap."""

    def _bump_and_check_revision_cap(
        self,
        task: Task,
        *,
        on_exhausted: Callable[[int], _T],
        on_continue: Callable[[int], _T],
    ) -> _T:
        """Bump ``task``'s revision count and branch on whether the shared cap is reached.

        Owns only the bump/compare/branch: it does not itself persist anything or log —
        callers each have their own accept-as-is-vs-fail side effects, log messages, and
        persisted fields, supplied via the two callbacks.

        Preconditions:
            - ``task`` is a non-terminal task tracked by this swarm's graph, about to be
              bounced this round (feedback for this round has already been persisted by
              the caller, e.g. via ``_escalate_if_no_change``).
            - ``on_exhausted`` and ``on_continue`` each accept the freshly bumped
              ``revision_count`` (an int) and perform the caller's side effects (graph
              writes, logging, cascade) and return the caller's return value.
        Postconditions:
            - Calls exactly one of ``on_exhausted`` (when the bumped count reaches
              ``coding_team_orchestrator.MAX_TASK_REVISIONS``) or ``on_continue``
              (otherwise), passing the bumped count, and returns that callback's result.
            - Does not itself call ``self.graph.update_task`` or log — that remains the
              callback's responsibility, preserving each caller's exact persisted fields
              and log message.
        """
        from software_engineering_team import coding_team_orchestrator as _orch

        revision_count = task.revision_count + 1
        if revision_count >= _orch.MAX_TASK_REVISIONS:
            return on_exhausted(revision_count)
        return on_continue(revision_count)
