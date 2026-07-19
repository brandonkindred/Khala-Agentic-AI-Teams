"""
Generic provisioner tool agent template.

Base implementation that can be extended for custom tools.
"""

from typing import Any, Dict, List, Optional, Tuple

from ..models import (
    AccessVerification,
    DeprovisionResult,
    GeneratedCredentials,
    ToolProvisionResult,
)
from ..shared.fencing import StaleFencingTokenError
from ..shared.provisioner_state import ProvisionerStateStore
from .base import BaseToolProvisioner


class GenericProvisionerTool(BaseToolProvisioner):
    """
    Generic tool provisioner template.

    This can be used for tools that don't require special provisioning logic,
    or as a base for implementing custom provisioners.

    The generic provisioner:
    1. Stores credentials without applying them
    2. Returns success with provided permissions
    3. Tracks provisioning state for verification
    """

    tool_name = "generic"

    def __init__(self, tool_name: str = "generic") -> None:
        self.tool_name = tool_name
        # Per-tool-name namespacing so two GenericProvisionerTool instances with
        # different ``tool_name`` don't collide in the shared state dir.
        self._state = ProvisionerStateStore(f"generic_{tool_name}_provisioner")

    def provision(
        self,
        agent_id: str,
        config: Dict[str, Any],
        credentials: GeneratedCredentials,
        fencing_token: Optional[int] = None,
    ) -> ToolProvisionResult:
        """Provision access for the agent (generic implementation).

        Stores the provisioning info but doesn't perform actual external
        operations. Override this method for real integrations.
        """
        return self.run_idempotent(
            agent_id,
            credentials=credentials,
            create=lambda _register: self._do_provision(config, credentials),
            hydrate_extras=("tool_name", "config"),
            fencing_token=fencing_token,
        )

    def _do_provision(
        self,
        config: Dict[str, Any],
        credentials: GeneratedCredentials,
    ) -> Tuple[List[str], Dict[str, Any]]:
        # Generic tool grants whatever the manifest declares; with tiers
        # gone (#456), anything in ``config["permissions"]`` is honoured
        # verbatim and an empty default means "everything the integration
        # exposes".
        permissions = list(config.get("permissions", ["all"]))

        credentials.extra["tool_name"] = self.tool_name
        credentials.extra["config"] = config

        details = {
            "tool_name": self.tool_name,
            "config": config,
            "config_applied": True,
            "permissions": permissions,
        }
        return permissions, details

    def verify_access(self, agent_id: str) -> AccessVerification:
        """Surface the recorded permissions for the agent."""
        prov_info = self._state.get(agent_id)

        if not prov_info:
            return self._make_verification(
                passed=False,
                actual_permissions=[],
                errors=[f"No provisioning found for agent {agent_id}"],
            )

        return self._make_verification(
            passed=True,
            actual_permissions=prov_info.get("permissions", []),
        )

    def deprovision(self, agent_id: str, fencing_token: Optional[int] = None) -> DeprovisionResult:
        """Remove agent access (generic implementation)."""
        if fencing_token is not None:
            self._state.check_fencing_token(agent_id, fencing_token)

        prov_info = self._state.get(agent_id)

        if not prov_info:
            return DeprovisionResult(
                tool_name=self.tool_name,
                success=True,
                details={"message": "No provisioning to remove"},
            )

        try:
            self._state.delete(agent_id, fencing_token=fencing_token)

            return DeprovisionResult(
                tool_name=self.tool_name,
                success=True,
                details={"agent_id": agent_id, "deprovisioned": True},
            )

        except StaleFencingTokenError:
            # A stale-token rejection from the fenced _state.delete is an
            # ownership error, not an infra failure: propagate it (non-retryable)
            # instead of folding it into a soft success=False result.
            raise
        except Exception as e:
            return DeprovisionResult(
                tool_name=self.tool_name,
                success=False,
                error=str(e),
            )


def create_custom_provisioner(
    tool_name: str,
    provision_fn: Optional[callable] = None,
    verify_fn: Optional[callable] = None,
    deprovision_fn: Optional[callable] = None,
) -> GenericProvisionerTool:
    """
    Factory function to create a custom provisioner with custom logic.

    Args:
        tool_name: Name of the tool
        provision_fn: Optional custom provision function
        verify_fn: Optional custom verification function
        deprovision_fn: Optional custom deprovision function

    Returns:
        Configured GenericProvisionerTool instance
    """
    provisioner = GenericProvisionerTool(tool_name)

    if provision_fn:
        provisioner.provision = lambda *args, **kwargs: provision_fn(provisioner, *args, **kwargs)
    if verify_fn:
        provisioner.verify_access = lambda *args, **kwargs: verify_fn(provisioner, *args, **kwargs)
    if deprovision_fn:
        provisioner.deprovision = lambda *args, **kwargs: deprovision_fn(
            provisioner, *args, **kwargs
        )

    return provisioner
