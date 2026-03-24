"""
Generic provisioner tool agent template.

Base implementation that can be extended for custom tools.
"""

from typing import Any, Dict, Optional

from ..models import (
    AccessTier,
    AccessVerification,
    DeprovisionResult,
    GeneratedCredentials,
    ToolProvisionResult,
)
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
        self._provisioned: Dict[str, Dict[str, Any]] = {}

    def provision(
        self,
        agent_id: str,
        config: Dict[str, Any],
        credentials: GeneratedCredentials,
        access_tier: AccessTier,
    ) -> ToolProvisionResult:
        """Provision access for the agent (generic implementation).

        This stores the provisioning info but doesn't perform actual
        external operations. Override this method for real integrations.
        """
        try:
            permissions = config.get("permissions", [access_tier.value])

            credentials.extra["tool_name"] = self.tool_name
            credentials.extra["config"] = config

            self._provisioned[agent_id] = {
                "config": config,
                "access_tier": access_tier.value,
                "permissions": permissions,
            }

            return self._make_success_result(
                credentials=credentials,
                permissions=permissions,
                details={
                    "tool_name": self.tool_name,
                    "access_tier": access_tier.value,
                    "config_applied": True,
                },
            )

        except Exception as e:
            return self._make_error_result(f"Generic provisioning error: {str(e)}")

    def verify_access(
        self,
        agent_id: str,
        expected_tier: AccessTier,
    ) -> AccessVerification:
        """Verify access for the agent (generic implementation)."""
        prov_info = self._provisioned.get(agent_id)

        if not prov_info:
            return self._make_verification(
                passed=False,
                expected_tier=expected_tier,
                actual_permissions=[],
                errors=[f"No provisioning found for agent {agent_id}"],
            )

        actual_tier = prov_info.get("access_tier", "standard")
        actual_permissions = prov_info.get("permissions", [])

        passed = actual_tier == expected_tier.value
        warnings = []

        if not passed:
            warnings.append(
                f"Access tier mismatch: expected {expected_tier.value}, got {actual_tier}"
            )

        return self._make_verification(
            passed=passed,
            expected_tier=expected_tier,
            actual_permissions=actual_permissions,
            warnings=warnings,
        )

    def deprovision(self, agent_id: str) -> DeprovisionResult:
        """Remove agent access (generic implementation)."""
        prov_info = self._provisioned.get(agent_id)

        if not prov_info:
            return DeprovisionResult(
                tool_name=self.tool_name,
                success=True,
                details={"message": "No provisioning to remove"},
            )

        try:
            del self._provisioned[agent_id]

            return DeprovisionResult(
                tool_name=self.tool_name,
                success=True,
                details={"agent_id": agent_id, "deprovisioned": True},
            )

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
