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
from typing import Any, Iterable, Optional

from shared.temporal.worker import start_team_worker

logger = logging.getLogger(__name__)

# team_slug -> module dotted path exporting WORKFLOWS / ACTIVITIES
TEAM_TEMPORAL_MODULES: dict[str, str] = {
    # Already-Temporal teams are registered via their own startup hooks; this
    # registry covers teams migrated by the shared.temporal rollout.
    "market_research": "market_research_team.temporal",
    "personal_assistant": "personal_assistant_team.temporal",
    "accessibility_audit": "accessibility_audit_team.temporal",
    "branding": "branding_team.temporal",
    "investment": "investment_team.temporal",
    "sales": "sales_team.temporal",
    "road_trip_planning": "road_trip_planning_team.temporal",
    "startup_advisor": "startup_advisor.temporal",
    "user_agent_founder": "agent_team_studio.user_agent_founder.temporal",
    "agentic_team_provisioning": "agent_team_studio.agentic_team_provisioning.temporal",
    "deepthought": "deepthought.temporal",
    "coding_team": "software_engineering_team.temporal.coding_team_workflow",
    "agent_provisioning": "agent_team_studio.agent_provisioning_team.temporal",
    "job_matching": "job_matching_team.temporal",
    "soc2_compliance": "soc2_compliance_team.temporal",
    # The code review agent runs Temporal by default; its worker serves the
    # code_review-queue used by ``CodeReviewWorkflow`` and its map/verify/reduce
    # activities (see ``software_engineering_team/code_review_agent/temporal``).
    "code_review": "software_engineering_team.code_review_agent.temporal",
}


def _resolve_task_queue(team: str, mod: Any) -> str:
    """The task queue to start ``team``'s worker on.

    Prefers the team module's own ``resolve_task_queue()`` (an operator
    override, e.g. SOC2's ``TEMPORAL_TASK_QUEUE_SOC2``) when it exports one, so
    a worker started through this generic host still polls the same queue
    ``start_workflow_sync`` dispatches to. Otherwise falls back to the
    module's own ``TASK_QUEUE`` constant when present — some teams (e.g. PA,
    draining a pre-existing ``personal-assistant`` queue) intentionally pin a
    fixed queue name that does not follow the ``f"{team}-queue"`` convention —
    and only then to that registry-default convention for teams that
    customize neither.
    """
    resolver = getattr(mod, "resolve_task_queue", None)
    if callable(resolver):
        return resolver()
    return getattr(mod, "TASK_QUEUE", f"{team}-queue")


def _resolve_max_concurrent_activities(mod: Any) -> Optional[int]:
    """The activity-slot count to start ``team``'s worker with, if customized.

    Prefers the team module's own ``MAX_CONCURRENT_ACTIVITIES`` int constant
    (e.g. SOC2's, sized for its 5-way criterion fan-out) when it exports one,
    so a worker started through this generic host has the same concurrency as
    the team's own dedicated boot hook — ``start_team_worker``'s default of 4
    can otherwise leave a wide fan-out queued behind other activities for a
    meaningful chunk of its schedule-to-close budget. Returns ``None`` for
    teams that don't customize it, so the caller omits the argument and
    ``start_team_worker``'s own default applies (avoids duplicating that
    default as a second literal here, which could drift from it).
    """
    value = getattr(mod, "MAX_CONCURRENT_ACTIVITIES", None)
    return value if isinstance(value, int) else None


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
            worker_kwargs: dict[str, Any] = {"task_queue": _resolve_task_queue(team, mod)}
            max_concurrent = _resolve_max_concurrent_activities(mod)
            if max_concurrent is not None:
                worker_kwargs["max_concurrent_activities"] = max_concurrent
            started = start_team_worker(team, workflows=workflows, activities=activities, **worker_kwargs)
            results[team] = started
        except Exception as e:
            logger.exception("Failed to start Temporal worker for %s: %s", team, e)
            results[team] = False
    return results
