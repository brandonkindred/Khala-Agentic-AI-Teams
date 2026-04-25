"""Target-team adapter registry.

The founder orchestrator looks up the adapter for a run by
``target_team_key``; each adapter satisfies the
``TargetTeamAdapter`` Protocol from ``base.py``.
"""

from __future__ import annotations

from user_agent_founder.targets.base import StartFailed, TargetTeamAdapter
from user_agent_founder.targets.software_engineering import SoftwareEngineeringAdapter

DEFAULT_TARGET_TEAM_KEY = "software_engineering"

ADAPTERS: dict[str, type[TargetTeamAdapter]] = {
    DEFAULT_TARGET_TEAM_KEY: SoftwareEngineeringAdapter,
}


def get_adapter(team_key: str) -> TargetTeamAdapter:
    """Return a fresh adapter instance for ``team_key``.

    Raises ``ValueError`` for an unknown team — the registry is the
    single source of truth for which teams the persona framework can
    drive.
    """
    if team_key not in ADAPTERS:
        raise ValueError(f"Team {team_key!r} does not support persona testing")
    return ADAPTERS[team_key]()


__all__ = [
    "ADAPTERS",
    "DEFAULT_TARGET_TEAM_KEY",
    "SoftwareEngineeringAdapter",
    "StartFailed",
    "TargetTeamAdapter",
    "get_adapter",
]
