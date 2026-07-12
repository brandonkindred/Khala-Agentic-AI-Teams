"""Central registry of all team Temporal modules.

Each entry maps a team slug to the dotted path of its ``temporal`` package,
which must export ``WORKFLOWS`` and ``ACTIVITIES``. ``start_all_team_workers``
imports each lazily and spins up one worker per team on its own task queue
so failures are isolated.

Each team's task queue is its own module's ``TASK_QUEUE`` constant when
present (falling back to ``f"{team}-queue"`` otherwise) — this lets a team
pin a fixed/legacy queue name (e.g. for a workflow.patched drain, or to match
a pre-existing external queue) and still register normally here, instead of
special-casing itself out of this registry.

Likewise, a team's own module's ``MAX_CONCURRENT_ACTIVITIES`` constant, when
present, overrides ``start_team_worker``'s default concurrency cap. This
matters because ``start_team_worker`` is idempotent per team name: whichever
caller starts a team's worker FIRST wins for the whole process. A team with
its own dedicated boot hook (e.g. a docker-compose ``TEAM_TEMPORAL_WORKER_FUNC``)
that pins a non-default cap there must export that SAME value here too, or a
consolidated process calling ``start_all_team_workers`` before that hook runs
would silently start the worker at the default cap instead, and the team's
own hook would then no-op (the worker is already running) without ever
re-pinning it.
"""

from __future__ import annotations

import importlib
import logging
from typing import Iterable

from shared_temporal.worker import start_team_worker

logger = logging.getLogger(__name__)

# team_slug -> module dotted path exporting WORKFLOWS / ACTIVITIES
TEAM_TEMPORAL_MODULES: dict[str, str] = {
    # Already-Temporal teams are registered via their own startup hooks; this
    # registry covers teams migrated by the shared_temporal rollout.
    "market_research": "market_research_team.temporal",
    "personal_assistant": "personal_assistant_team.temporal",
    "accessibility_audit": "accessibility_audit_team.temporal",
    "branding": "branding_team.temporal",
    "investment": "investment_team.temporal",
    "sales": "sales_team.temporal",
    "road_trip_planning": "road_trip_planning_team.temporal",
    "startup_advisor": "startup_advisor.temporal",
    "user_agent_founder": "user_agent_founder.temporal",
    "agentic_team_provisioning": "agentic_team_provisioning.temporal",
    "deepthought": "deepthought.temporal",
    "coding_team": "coding_team.temporal",
    "agent_provisioning": "agent_provisioning_team.temporal",
    "job_matching": "job_matching_team.temporal",
    # The code review agent runs Temporal by default; its worker serves the
    # code_review-queue used by ``CodeReviewWorkflow`` and its map/verify/reduce
    # activities (see ``software_engineering_team/code_review_agent/temporal``).
    "code_review": "software_engineering_team.code_review_agent.temporal",
}


def start_all_team_workers(only: Iterable[str] | None = None) -> dict[str, bool]:
    """Start a Temporal worker for every registered team.

    Preconditions:
        - ``only``, if given, is an iterable of team slugs; slugs not present
          in ``TEAM_TEMPORAL_MODULES`` are silently ignored (not an error).

    Postconditions:
        - Returns a map of team -> whether a worker thread was started, one
          entry per team considered (all of ``TEAM_TEMPORAL_MODULES`` when
          ``only`` is ``None``, else its intersection with ``only``).
        - A team whose Temporal module fails to import, or exposes no/empty
          ``WORKFLOWS``/``ACTIVITIES``, is skipped with an error/warning log
          and mapped to ``False`` rather than raising and blocking startup of
          the remaining teams.
        - A team exporting ``MAX_CONCURRENT_ACTIVITIES`` starts its worker
          with that cap instead of ``start_team_worker``'s default, so this
          bulk path agrees with that team's own dedicated boot hook on
          whichever one wins the idempotent-start race.
    """
    results: dict[str, bool] = {}
    teams = TEAM_TEMPORAL_MODULES.items()
    if only is not None:
        wanted = set(only)
        teams = [(t, m) for t, m in teams if t in wanted]

    for team, module_path in teams:
        try:
            mod = importlib.import_module(module_path)
            workflows = getattr(mod, "WORKFLOWS", None)
            activities = getattr(mod, "ACTIVITIES", None)
            if not workflows or not activities:
                logger.warning(
                    "Team %s module %s missing WORKFLOWS/ACTIVITIES; skipping",
                    team,
                    module_path,
                )
                results[team] = False
                continue
            kwargs = {"task_queue": getattr(mod, "TASK_QUEUE", f"{team}-queue")}
            max_concurrent_activities = getattr(mod, "MAX_CONCURRENT_ACTIVITIES", None)
            if max_concurrent_activities is not None:
                # Omitted entirely (rather than passed as a literal default)
                # when a team defines no override, so start_team_worker's own
                # default stays the single source of truth for "no opinion".
                kwargs["max_concurrent_activities"] = max_concurrent_activities
            started = start_team_worker(
                team,
                workflows=workflows,
                activities=activities,
                **kwargs,
            )
            results[team] = started
        except Exception as e:
            logger.exception("Failed to start Temporal worker for %s: %s", team, e)
            results[team] = False
    return results
