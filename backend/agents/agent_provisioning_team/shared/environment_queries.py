"""Read-only environment status helpers shared by API and orchestrator."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent_provisioning_team.shared.environment_store import EnvironmentStore


def get_agent_status_dict(
    store: EnvironmentStore,
    agent_id: str,
) -> Optional[Dict[str, Any]]:
    """Build a status dict for ``agent_id``, or ``None`` if missing.

    Preconditions:
        * ``agent_id`` is non-empty.
    Postconditions:
        * Returns a dict with agent/container/tools fields when present.
    """
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
    }


def list_agent_status_dicts(
    store: EnvironmentStore,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List agent status dicts, optionally filtered by ``status``.

    Preconditions:
        * ``status``, when set, matches stored environment status strings.
    Postconditions:
        * Returns zero or more status dicts.
    """
    return [
        {
            "agent_id": env.agent_id,
            "status": env.status,
            "container_name": env.container_name,
            "tools_provisioned": env.tools_provisioned,
            "created_at": env.created_at,
        }
        for env in store.list_all(status=status)
    ]
