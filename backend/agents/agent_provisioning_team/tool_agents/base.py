"""
Base interface for tool provisioner agents.

All tool provisioners implement this protocol to ensure consistent behavior.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from ..models import (
    AccessTier,
    AccessVerification,
    DeprovisionResult,
    GeneratedCredentials,
    ToolConfig,
    ToolProvisionResult,
)


@runtime_checkable
class ToolProvisionerInterface(Protocol):
    """Protocol for tool provisioning agents."""

    def provision(
        self,
        agent_id: str,
        config: Dict[str, Any],
        credentials: GeneratedCredentials,
        access_tier: AccessTier,
    ) -> ToolProvisionResult:
        """Provision resources for the agent in this tool.
        
        Args:
            agent_id: Unique identifier for the agent
            config: Tool-specific configuration from manifest
            credentials: Pre-generated credentials to use
            access_tier: Requested access tier
        
        Returns:
            ToolProvisionResult with success status and details
        """
        ...

    def verify_access(
        self,
        agent_id: str,
        expected_tier: AccessTier,
    ) -> AccessVerification:
        """Verify the agent's access matches expected permissions.
        
        Args:
            agent_id: Agent to verify
            expected_tier: Expected access tier
        
        Returns:
            AccessVerification result
        """
        ...

    def deprovision(self, agent_id: str) -> DeprovisionResult:
        """Remove agent's access and clean up resources.
        
        Args:
            agent_id: Agent to deprovision
        
        Returns:
            DeprovisionResult with success status
        """
        ...


class BaseToolProvisioner(ABC):
    """Base class for tool provisioners with common functionality."""

    tool_name: str = "base"

    @abstractmethod
    def provision(
        self,
        agent_id: str,
        config: Dict[str, Any],
        credentials: GeneratedCredentials,
        access_tier: AccessTier,
    ) -> ToolProvisionResult:
        """Provision resources for the agent."""
        pass

    @abstractmethod
    def verify_access(
        self,
        agent_id: str,
        expected_tier: AccessTier,
    ) -> AccessVerification:
        """Verify agent access matches expected tier."""
        pass

    @abstractmethod
    def deprovision(self, agent_id: str) -> DeprovisionResult:
        """Remove agent access and resources."""
        pass

    def _make_success_result(
        self,
        credentials: GeneratedCredentials,
        permissions: List[str],
        details: Optional[Dict[str, Any]] = None,
    ) -> ToolProvisionResult:
        """Create a successful provision result."""
        return ToolProvisionResult(
            tool_name=self.tool_name,
            success=True,
            credentials=credentials,
            permissions=permissions,
            details=details or {},
        )

    def _make_error_result(self, error: str) -> ToolProvisionResult:
        """Create an error provision result."""
        return ToolProvisionResult(
            tool_name=self.tool_name,
            success=False,
            error=error,
        )

    def _make_verification(
        self,
        passed: bool,
        expected_tier: AccessTier,
        actual_permissions: List[str],
        warnings: Optional[List[str]] = None,
        errors: Optional[List[str]] = None,
    ) -> AccessVerification:
        """Create an access verification result."""
        return AccessVerification(
            tool_name=self.tool_name,
            passed=passed,
            expected_tier=expected_tier.value,
            actual_permissions=actual_permissions,
            warnings=warnings or [],
            errors=errors or [],
        )
