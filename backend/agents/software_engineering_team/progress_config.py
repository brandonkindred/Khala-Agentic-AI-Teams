"""Progress-band math and concurrency/cap configuration for the coding-team swarm.

Extracted from ``coding_team/orchestrator.py`` (decompose the orchestrator god-file
into named collaborators) — pure structural move, no behavior change.
"""

from __future__ import annotations

from typing import Any, Dict, List

from shared_env import parse_int

# Cap on CONSECUTIVE no-change revision rounds — rounds where the engineer revisited a task it
# already flagged done and produced no change to the branch diff. This is deliberately distinct from
# MAX_TASK_REVISIONS: a revision that actually changes the code resets this counter and keeps the
# full revision budget, so productive work is never throttled. Only zero-progress re-evaluation is
# bounded — on reaching the cap the task is handed to the Tech Lead for direction instead of being
# bounced again.
NO_CHANGE_REVISIT_CAP = 3


def _no_change_revisit_cap() -> int:
    """Consecutive no-change revision rounds tolerated before escalating to the Tech Lead.

    Configurable via CODING_TEAM_NO_CHANGE_REVISIT_CAP (default 3; garbage/empty → default; floored
    at 1 so the guard can never be disabled into an unbounded no-progress loop).

    Preconditions:
        - None (reads only the optional environment variable).
    Postconditions:
        - Returns an int >= 1.
    """
    return parse_int("CODING_TEAM_NO_CHANGE_REVISIT_CAP", NO_CHANGE_REVISIT_CAP, minimum=1)


# Max Tech-Lead review LLM calls dispatched concurrently in one review round. Reviews are
# independent (read-only diff + an LLM call), so a round with k tasks in review costs ~one review
# latency instead of k. The effective pool is min(this, number of tasks in review).
REVIEW_CONCURRENCY = 4


def _review_concurrency() -> int:
    """Max concurrent Tech-Lead reviews per round.

    Configurable via CODING_TEAM_REVIEW_CONCURRENCY (default 4; garbage/empty → default; floored at
    1 so review always makes progress even if the value is set to 0/negative).

    Preconditions:
        - None (reads only the optional environment variable).
    Postconditions:
        - Returns an int >= 1.
    """
    return parse_int("CODING_TEAM_REVIEW_CONCURRENCY", REVIEW_CONCURRENCY, minimum=1)


# Max Tech-Lead task-grooming LLM calls dispatched concurrently right after planning. Grooming is
# independent per task (each call is driven only by that task's own id/title/description/
# dependencies plus the shared plan context), so a task graph with k tasks costs ~one grooming
# latency instead of k. The effective pool is min(this, number of planned tasks).
GROOM_CONCURRENCY = 4


def _groom_concurrency() -> int:
    """Max concurrent Tech-Lead task-grooming calls right after planning.

    Configurable via CODING_TEAM_GROOM_CONCURRENCY (default 4; garbage/empty → default; floored at
    1 so grooming always makes progress even if the value is set to 0/negative).

    Preconditions:
        - None (reads only the optional environment variable).
    Postconditions:
        - Returns an int >= 1.
    """
    return parse_int("CODING_TEAM_GROOM_CONCURRENCY", GROOM_CONCURRENCY, minimum=1)


# Max implementation workers dispatched concurrently in one round. Each worker operates in its
# own git worktree (see coding_team.worktree_manager), so concurrent workers no longer share (and
# corrupt) a single working tree. The effective pool is min(this, number of workers with an
# assigned task this round) — the default 2-worker roster (frontend_v2/backend_v2) rarely reaches
# this ceiling.
IMPLEMENTATION_CONCURRENCY = 4


def _implementation_concurrency() -> int:
    """Max concurrent implementation workers per round.

    Configurable via CODING_TEAM_IMPLEMENTATION_CONCURRENCY (default 4; garbage/empty → default;
    floored at 1 so implementation always makes progress even if the value is set to 0/negative).

    Preconditions:
        - None (reads only the optional environment variable).
    Postconditions:
        - Returns an int >= 1.
    """
    return parse_int(
        "CODING_TEAM_IMPLEMENTATION_CONCURRENCY", IMPLEMENTATION_CONCURRENCY, minimum=1
    )


# Default job-level progress band for the coding phase. The caller owns the band
# allocation: the parent pipeline (software_engineering_team) maps its earlier phases
# onto lower sub-ranges and passes the coding team its slice via the
# ``progress_base``/``progress_span`` parameters; a standalone coding-team job uses
# the full bar. Terminal completion always writes 100.
_DEFAULT_PROGRESS_BASE = 0
_DEFAULT_PROGRESS_SPAN = 95


def _coding_progress(tasks: List[Dict[str, Any]], base: int, span: int) -> int:
    """Map the terminal-task share onto the job progress band [base, base + span].

    Preconditions:
        - ``tasks`` are snapshot dicts whose ``status`` is a TaskStatus value string.
        - ``0 <= base``, ``0 <= span``, ``base + span <= 100``.
    Postconditions:
        - Returns an int in [base, base + span]; an empty graph yields the base (no
          division by zero). Monotone in the number of terminal (merged/failed)
          tasks, so within the coding phase the bar only ever advances.
    """
    assert 0 <= base and 0 <= span and base + span <= 100, (base, span)
    total = len(tasks)
    if total == 0:
        return base
    done = sum(1 for t in tasks if t.get("status") in ("merged", "failed"))
    return base + int(span * done / total)
