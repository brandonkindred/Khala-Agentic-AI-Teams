"""Target-team adapter registry.

The founder orchestrator looks up the adapter for a run by
``target_team_key``; each adapter satisfies the
``TargetTeamAdapter`` Protocol from ``base.py``.
"""

from __future__ import annotations

from agent_team_studio.user_agent_founder.targets.agentic_team import AgenticTeamAdapter
from agent_team_studio.user_agent_founder.targets.base import StartFailed, TargetTeamAdapter
from agent_team_studio.user_agent_founder.targets.software_engineering import (
    SoftwareEngineeringAdapter,
)

DEFAULT_TARGET_TEAM_KEY = "software_engineering"

# Statically-registered, single-instance target teams. Agentic teams are *not*
# listed here — they are addressed dynamically by id (see ``AGENTIC_TEAM_PREFIX``)
# because there is one per provisioned team, created at runtime.
ADAPTERS: dict[str, type[TargetTeamAdapter]] = {
    DEFAULT_TARGET_TEAM_KEY: SoftwareEngineeringAdapter,
}

# Prefix that marks a dynamic agentic-team target key: ``agentic_team:<team_id>``.
AGENTIC_TEAM_PREFIX = "agentic_team:"


def get_adapter(
    team_key: str, *, process_id: str | None = None, spec: str | None = None
) -> TargetTeamAdapter:
    """Return a fresh adapter instance for ``team_key``.

    Handles both static registry keys (e.g. ``"software_engineering"``) and the
    dynamic ``"agentic_team:<team_id>"`` form, which builds an
    :class:`AgenticTeamAdapter` for that team. ``process_id`` is the process the
    persona should drive; ``spec`` seeds the adapter's analysis→build pass-through
    for the resume path (see ``AgenticTeamAdapter``). Both are required only for
    agentic-team keys and ignored for the static targets (which have neither a
    process nor a collapsed analysis phase).

    This function does **not** enforce that ``process_id`` is present for
    agentic-team keys — it threads whatever is given (including ``None``) into the
    adapter, which raises :class:`StartFailed` at build time if it is still
    missing. ``/start`` validates ``process_id`` up front for a clearer 400 (a
    failed run is a worse experience); this is the last-resort guard, not the
    first.

    Preconditions: ``team_key`` is non-empty; for an agentic-team key the id
        after the prefix is non-empty.
    Postconditions: returns an adapter satisfying :class:`TargetTeamAdapter`.
        Raises ``ValueError`` for an unknown static key or a malformed
        agentic-team key — the registry/parser is the single source of truth for
        which teams the persona framework can drive.
    """
    if not team_key:
        raise ValueError("get_adapter: team_key must be non-empty")
    if team_key.startswith(AGENTIC_TEAM_PREFIX):
        team_id = team_key[len(AGENTIC_TEAM_PREFIX) :]
        if not team_id:
            raise ValueError(f"Malformed agentic-team key {team_key!r}: missing team id")
        return AgenticTeamAdapter(team_id, process_id=process_id, spec=spec)
    if team_key not in ADAPTERS:
        raise ValueError(f"Team {team_key!r} does not support persona testing")
    return ADAPTERS[team_key]()


__all__ = [
    "ADAPTERS",
    "AGENTIC_TEAM_PREFIX",
    "AgenticTeamAdapter",
    "DEFAULT_TARGET_TEAM_KEY",
    "SoftwareEngineeringAdapter",
    "StartFailed",
    "TargetTeamAdapter",
    "get_adapter",
]
