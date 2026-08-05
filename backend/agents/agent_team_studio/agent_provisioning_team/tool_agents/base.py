"""
Base interface for tool provisioner agents.

All tool provisioners implement this protocol to ensure consistent behavior.
"""

import logging
import subprocess
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from ..models import (
    AccessVerification,
    DeprovisionResult,
    GeneratedCredentials,
    ToolProvisionResult,
)
from ..shared.fencing import StaleFencingTokenError
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
        fencing_token: Optional[int] = None,
    ) -> ToolProvisionResult:
        """Provision resources for the agent in this tool.

        Args:
            agent_id: Unique identifier for the agent
            config: Tool-specific configuration from manifest
            credentials: Pre-generated credentials to use
            fencing_token: Caller's fencing token (see
                ``shared.fencing``); ``None`` skips enforcement.

        Returns:
            ToolProvisionResult with success status and details
        """
        ...

    def verify_access(
        self,
        agent_id: str,
    ) -> AccessVerification:
        """Verify the agent's access is in place.

        Args:
            agent_id: Agent to verify

        Returns:
            AccessVerification result
        """
        ...

    def deprovision(self, agent_id: str, fencing_token: Optional[int] = None) -> DeprovisionResult:
        """Remove agent's access and clean up resources.

        Args:
            agent_id: Agent to deprovision
            fencing_token: Caller's fencing token (see
                ``shared.fencing``); ``None`` skips enforcement.

        Returns:
            DeprovisionResult with success status
        """
        ...


class BaseToolProvisioner(ABC):
    """Base class for tool provisioners with common functionality.

    Provisioned environments host AI agents that must follow the canonical anatomy
    in ``agent_team_studio.agent_provisioning_team.AGENT_ANATOMY.md``; use
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
        fencing_token: Optional[int] = None,
    ) -> ToolProvisionResult:
        """Provision resources for the agent."""
        pass

    @abstractmethod
    def verify_access(
        self,
        agent_id: str,
    ) -> AccessVerification:
        """Verify agent access is in place."""
        pass

    @abstractmethod
    def deprovision(self, agent_id: str, fencing_token: Optional[int] = None) -> DeprovisionResult:
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
        on_persist_failure: Optional[Callable[[Dict[str, Any]], None]] = None,
        fencing_token: Optional[int] = None,
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
          - ``reuse`` may also raise to reject stale stored state instead of
            trusting it (e.g. a provisioner that confirms the underlying
            resource no longer exists): the exception is caught by the same
            handling as ``create``'s, producing an error result instead of a
            silently-wrong success.
        * Exceptions from infrastructure boundaries (missing binaries, subprocess
          timeouts, permission errors) are caught and converted to error results.
          Compensation records already registered before the exception remain
          persisted for the orchestrator to replay. Domain validation failures
          should ``return self._make_error_result(...)`` from inside ``create``.
        * ``on_persist_failure(details)`` runs when ``create`` succeeds but the
          follow-up ``state.put`` then raises (e.g. a full or read-only cache).
          ``details`` is exactly what ``create`` just returned, so a provisioner
          that created an out-of-band resource (a container, a role) can use it
          to tear that resource down directly by name — the store never
          recorded it, so the normal state-lookup-based ``deprovision(agent_id)``
          path has nothing to find. Exceptions from ``on_persist_failure`` are
          logged and swallowed so cleanup can never mask the original
          persistence failure, which still propagates to the error-result
          translation below. Not invoked when ``state.put`` raises
          ``StaleFencingTokenError`` (see below) — that failure always
          propagates immediately, since auto-removing the resource this call
          just created could race a legitimate new owner discovering and
          adopting the very same resource.
        * ``fencing_token``, when given, is checked against ``self._state``
          *before* ``create`` runs — i.e. before any real infrastructure
          mutation, not just before the final state persist — so a stale
          caller's write is rejected before it can touch live infrastructure.
          A rejection raises
          :class:`~agent_team_studio.agent_provisioning_team.shared.fencing.StaleFencingTokenError`
          (propagated, not converted to an error result: this is a
          programming/ownership error, not an infrastructure failure). The
          same rejection can also surface later, from the final
          ``state.put`` — ``create`` may run for a long time (e.g. a slow
          ``docker run``), long enough for a legitimate new owner to reclaim
          ``agent_id`` in the interim — and propagates identically either way.
          On the reuse path (``existing is not None``), which otherwise never
          calls ``state.put``, ``fencing_token`` is still persisted as the
          new high-water mark — a reuse is a real, validated touch by this
          caller, and skipping the persist would let a later call presenting
          a token between the old mark and this one wrongly pass as current
          against state this caller never actually validated against.
        """
        state = self._state

        def _register(kind: str, payload: Dict[str, Any]) -> None:
            state.add_compensation(
                agent_id,
                CompensationRecord(kind=kind, payload=payload),
                fencing_token=fencing_token,
            )

        try:
            if fencing_token is not None:
                state.check_fencing_token(agent_id, fencing_token)

            existing = state.get(agent_id)
            if existing is not None:
                if fencing_token is not None:
                    # The preflight above only checked the token; the reuse
                    # path returns without ever calling state.put(), so
                    # without this the stored high-water mark would stay at
                    # whatever it was before this call. A later call
                    # presenting a token between the old mark and this one
                    # would then wrongly pass as "not stale" against state
                    # this caller never actually validated against.
                    state.put(agent_id, existing, fencing_token=fencing_token)
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
            try:
                state.put(agent_id, details, fencing_token=fencing_token)
            except StaleFencingTokenError:
                raise
            except Exception:
                if on_persist_failure is not None:
                    try:
                        on_persist_failure(details)
                    except Exception:
                        logger.exception(
                            "%s: on_persist_failure cleanup raised for agent_id=%s",
                            self.tool_name,
                            agent_id,
                        )
                raise
            return self._make_success_result(
                credentials=credentials,
                permissions=permissions,
                details=details,
            )
        except StaleFencingTokenError:
            raise
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

    def clear_compensations(self, agent_id: str, fencing_token: Optional[int] = None) -> None:
        """Clear compensation records for ``agent_id`` (leaves details intact)."""
        self._state.clear_compensations(agent_id, fencing_token=fencing_token)

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
        actual_permissions: List[str],
        warnings: Optional[List[str]] = None,
        errors: Optional[List[str]] = None,
    ) -> AccessVerification:
        """Create an access verification result."""
        return AccessVerification(
            tool_name=self.tool_name,
            passed=passed,
            actual_permissions=actual_permissions,
            warnings=warnings or [],
            errors=errors or [],
        )
