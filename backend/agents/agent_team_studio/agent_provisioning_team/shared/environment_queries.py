"""Read-only environment status helpers shared by API and orchestrator."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore


def get_agent_status_dict(
    store: EnvironmentStore,
    agent_id: str,
) -> Optional[Dict[str, Any]]:
    """Build a status dict for ``agent_id``, or ``None`` if missing.

    Preconditions:
        * ``store`` is a non-``None`` ``EnvironmentStore``.
        * ``agent_id`` is non-empty.
    Postconditions:
        * Returns a dict with agent/container/tools fields when present.
        * Returns ``None`` when the agent is absent from ``store``.
    """
    assert store is not None, "store must be an EnvironmentStore instance"
    assert agent_id, "agent_id must be non-empty"
    env = store.get(agent_id)
    if env is None:
        return None
    return {
        "agent_id": agent_id,
        "status": env.status,
        "container_id": env.container_id,
        "container_name": env.container_name,
        "tools_provisioned": env.tools_provisioned,
        "created_at": env.created_at,
        "updated_at": env.updated_at,
    }


def list_agent_status_dicts(
    store: EnvironmentStore,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List agent status dicts, optionally filtered by ``status``.

    Preconditions:
        * ``store`` is a non-``None`` ``EnvironmentStore``.
        * ``status``, when set, matches stored environment status strings.
    Postconditions:
        * Returns zero or more status dicts.
    """
    assert store is not None, "store must be an EnvironmentStore instance"
    return [
        {
            "agent_id": env.agent_id,
            "status": env.status,
            "container_name": env.container_name,
            "tools_provisioned": env.tools_provisioned,
            "created_at": env.created_at,
            "updated_at": env.updated_at,
        }
        for env in store.list_all(status=status)
    ]
