"""
Central registry/factory for tool provisioner agents.

Single source of truth — replaces three duplicated `_build_provisioners()`
helpers previously living in orchestrator.py, phases/account_provisioning.py
and phases/access_audit.py.
"""

from __future__ import annotations

from typing import Dict

from ..tool_agents.base import ToolProvisionerInterface
from ..tool_agents.docker_provisioner import DockerProvisionerTool
from ..tool_agents.generic_provisioner import GenericProvisionerTool
from ..tool_agents.git_provisioner import GitProvisionerTool
from ..tool_agents.postgres_provisioner import PostgresProvisionerTool
from ..tool_agents.redis_provisioner import RedisProvisionerTool


def build_default_tool_agents() -> Dict[str, ToolProvisionerInterface]:
    """Build the default set of tool provisioner agents.

    Preconditions:
        * None. Takes no arguments and reads no external/mutable state; the
          concrete provisioner classes are import-time dependencies of this
          module.
    Postconditions:
        * Returns a ``dict`` mapping each provisioner key to a freshly
          constructed ``ToolProvisionerInterface`` instance. New instances are
          created on every call (no shared or cached state).
    Invariants:
        * Keys match the ``provisioner`` field used by tool manifests. They are
          also the canonical identifier stamped onto
          ``ToolProvisionResult.provisioner_key`` by ``run_account_provisioning``
          and consumed by ``ProvisioningOrchestrator.compensate()`` to look a
          provisioner back up for rollback. Do not rename a key here without
          updating any manifests, persisted results, and call sites that store
          or match on it.
    """
    return {
        "docker_provisioner": DockerProvisionerTool(),
        "postgres_provisioner": PostgresProvisionerTool(),
        "git_provisioner": GitProvisionerTool(),
        "redis_provisioner": RedisProvisionerTool(),
        "generic_provisioner": GenericProvisionerTool(),
    }
