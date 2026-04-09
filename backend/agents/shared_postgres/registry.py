"""Central registry of team Postgres schema modules.

Analogue of ``shared_temporal/teams_registry.py``. Each entry maps a
team slug to the dotted path of a module that exports ``SCHEMA:
TeamSchema``. ``register_all_team_schemas`` imports each module lazily
and applies its schema; it is not wired into the ``unified_api``
lifespan (teams run in their own containers and register themselves),
but is useful for CLI migrations, tests, and standalone harnesses.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterable
from typing import Optional

from shared_postgres.runner import register_team_schemas
from shared_postgres.schema import TeamSchema

logger = logging.getLogger(__name__)

# team_slug -> module dotted path exporting ``SCHEMA``
TEAM_POSTGRES_MODULES: dict[str, str] = {
    # unified_api owns the shared encrypted credentials table.
    "unified_api": "unified_api.postgres",
    # job_service owns the ``jobs`` table in the ``strands_jobs`` DB.
    "job_service": "job_service.postgres",
    # Teams with persistence being moved to Postgres.
    "branding": "branding_team.postgres",
    "startup_advisor": "startup_advisor.postgres",
    "user_agent_founder": "user_agent_founder.postgres",
    "team_assistant": "team_assistant.postgres",
    "agentic_team_provisioning": "agentic_team_provisioning.postgres",
    "blogging": "blogging.postgres",
}


def register_all_team_schemas(only: Optional[Iterable[str]] = None) -> dict[str, bool]:
    """Import each registered team module and apply its schema.

    Returns a ``{team: success}`` map. A team whose module fails to
    import is logged and recorded as ``False`` so one bad team doesn't
    block the rest. The ``only`` filter accepts an iterable of team
    slugs to restrict the run.
    """
    results: dict[str, bool] = {}
    teams: Iterable[tuple[str, str]] = TEAM_POSTGRES_MODULES.items()
    if only is not None:
        wanted = set(only)
        teams = [(t, m) for t, m in teams if t in wanted]

    for team, module_path in teams:
        try:
            mod = importlib.import_module(module_path)
            schema = getattr(mod, "SCHEMA", None)
            if not isinstance(schema, TeamSchema):
                logger.warning(
                    "Team %s module %s does not export a SCHEMA: TeamSchema; skipping",
                    team,
                    module_path,
                )
                results[team] = False
                continue
            results[team] = register_team_schemas(schema)
        except Exception as e:
            logger.exception("Failed to register postgres schema for %s: %s", team, e)
            results[team] = False
    return results
