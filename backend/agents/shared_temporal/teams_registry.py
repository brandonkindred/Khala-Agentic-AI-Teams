"""Central registry of all team Temporal modules.

Each entry maps a team slug to the dotted path of its ``temporal`` package,
which must export ``WORKFLOWS`` and ``ACTIVITIES``. ``start_all_team_workers``
imports each lazily and spins up one worker per team on its own task queue
so failures are isolated.
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
}


def start_all_team_workers(only: Iterable[str] | None = None) -> dict[str, bool]:
    """Start a Temporal worker for every registered team.

    Returns a map of team -> whether a worker thread was started. Teams
    whose Temporal module fails to import are skipped with an error log
    rather than blocking startup of the rest.
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
            started = start_team_worker(
                team,
                workflows=workflows,
                activities=activities,
                task_queue=f"{team}-queue",
            )
            results[team] = started
        except Exception as e:
            logger.exception("Failed to start Temporal worker for %s: %s", team, e)
            results[team] = False
    return results
