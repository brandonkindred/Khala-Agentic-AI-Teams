"""
Base interface for tool provisioner agents.

All tool provisioners implement this protocol to ensure consistent behavior.
"""

import logging
import subprocess
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from ..models import (
    AccessTier,
    AccessVerification,
    DeprovisionResult,
    GeneratedCredentials,
    ToolProvisionResult,
)
from ..shared.provisioner_state import CompensationRecord

logger = logging.getLogger(__name__)

# Callable passed into ``create(...)`` so provisioners can register per-step
# rollbacks as each side effect lands. ``kind`` is a stable, provisioner-
# namespaced string (e.g. ``"postgres.drop_database"``); ``payload`` must be
# JSON-serializable — the record is persisted write-through so a crash
# mid-provision can still be replayed on cold start.
CompensationRegistrar = Callable[[str, Dict[str, Any]], None]


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
    """Base class for tool provisioners with common functionality.

    Provisioned environments host AI agents that must follow the canonical anatomy
    in ``agent_provisioning_team.AGENT_ANATOMY.md``; use
    ``canonical_anatomy_prompt_preamble()`` when generating LLM-facing docs or designs.
    """

    tool_name: str = "base"

    @staticmethod
    def canonical_anatomy_prompt_preamble() -> str:
        """Full Khala agent anatomy text for prompts (AGENT_ANATOMY.md + diagram list)."""
        from ..anatomy_assets import get_anatomy_prompt_preamble

        return get_anatomy_prompt_preamble()

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

    def run_idempotent(
        self,
        agent_id: str,
        *,
        credentials: GeneratedCredentials,
        create: Callable[[CompensationRegistrar], Tuple[List[str], Dict[str, Any]]],
        hydrate_extras: Tuple[str, ...] = (),
        reuse: Optional[Callable[[Dict[str, Any]], List[str]]] = None,
    ) -> ToolProvisionResult:
        """Run ``create`` once per (provisioner, agent_id); reuse stored state on subsequent calls.

        State lookup, short-circuit on prior success, uniform exception → error-result
        translation, and persistence of the success payload all live here. Each
        provisioner's ``create`` function does only the tool-specific work.

        Contract:

        * ``create(register_compensation)`` returns ``(permissions, details)``.
          ``details`` is both returned in ``ToolProvisionResult.details`` and
          persisted as the idempotency state payload. It may mutate
          ``credentials`` in place.
        * ``register_compensation(kind, payload)`` records a LIFO rollback
          step, persisted write-through. Provisioners should call it *after*
          each destructive side effect lands (e.g. after ``CREATE DATABASE``
          succeeds, register ``"postgres.drop_database"``). On failure the
          orchestrator replays these in reverse via
          :meth:`replay_compensation`; provisioners that register nothing
          keep the legacy :meth:`deprovision` fallback.
        * On the reuse path:
          - ``hydrate_extras`` lists ``details`` keys whose stored values are
            copied into ``credentials.extra`` via ``setdefault`` — the common
            case ("restore what create populated"), so most provisioners don't
            need a ``reuse`` callback.
          - ``reuse(stored_details)`` is a full-control override: returns
            ``permissions`` and may mutate ``credentials`` arbitrarily. Used
            when the reuse path needs to consult live env (e.g. Postgres host
            from env, not the stored value) or recompute permissions from the
            current access tier.
          - When neither is enough to derive permissions, the default is
            ``stored_details.get("permissions", [])``.
        * Exceptions from infrastructure boundaries (missing binaries, subprocess
          timeouts, permission errors) are caught and converted to error results.
          Compensation records already registered before the exception remain
          persisted for the orchestrator to replay. Domain validation failures
          should ``return self._make_error_result(...)`` from inside ``create``.
        """
        state = self._state

        def _register(kind: str, payload: Dict[str, Any]) -> None:
            state.add_compensation(agent_id, CompensationRecord(kind=kind, payload=payload))

        try:
            existing = state.get(agent_id)
            if existing is not None:
                for key in hydrate_extras:
                    if key in existing:
                        credentials.extra.setdefault(key, existing[key])
                if reuse is not None:
                    permissions = reuse(existing)
                else:
                    permissions = list(existing.get("permissions", []))
                return self._make_success_result(
                    credentials=credentials,
                    permissions=permissions,
                    details={**existing, "reused": True},
                )

            permissions, details = create(_register)
            state.put(agent_id, details)
            return self._make_success_result(
                credentials=credentials,
                permissions=permissions,
                details=details,
            )
        except FileNotFoundError as e:
            return self._make_error_result(f"{self.tool_name}: required binary not found: {e}")
        except subprocess.TimeoutExpired:
            return self._make_error_result(f"{self.tool_name}: provisioning subprocess timed out")
        except PermissionError as e:
            return self._make_error_result(f"{self.tool_name}: permission denied: {e}")
        except Exception as e:  # noqa: BLE001 — last-resort guard with explicit prior cases
            return self._make_error_result(f"{self.tool_name} provisioning error: {e}")

    # ---- Compensation hooks ---------------------------------------------
    def list_compensations(self, agent_id: str) -> List[CompensationRecord]:
        """Return compensation records registered for ``agent_id``."""
        return self._state.list_compensations(agent_id)

    def clear_compensations(self, agent_id: str) -> None:
        """Clear compensation records for ``agent_id`` (leaves details intact)."""
        self._state.clear_compensations(agent_id)

    def replay_compensation(
        self,
        agent_id: str,
        kind: str,
        payload: Dict[str, Any],
    ) -> None:
        """Dispatch a single compensation record back to live infrastructure.

        Default: log a warning and skip. Provisioners that register
        compensations in ``create(...)`` must override this to map each
        ``kind`` back to the corresponding cleanup (e.g.
        ``"postgres.drop_database"`` → terminate sessions + ``DROP DATABASE``).
        """
        logger.warning(
            "%s: no replay handler for compensation kind=%r (agent=%s, payload keys=%s); skipping",
            self.tool_name,
            kind,
            agent_id,
            sorted(payload.keys()),
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
