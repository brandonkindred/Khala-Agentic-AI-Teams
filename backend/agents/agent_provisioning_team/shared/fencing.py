"""Fencing-token enforcement shared by every resource-mutating store.

A fencing token (``shared/agent_lock.py``'s ``AgentLockStore.acquire()``
return value) is a per-``agent_id`` monotonic counter. Every store that
mutates ``agent_id``-keyed resources on behalf of
``AgentProvisioningWorkflow`` / ``AgentDeprovisioningWorkflow`` /
``ProvisioningOrchestrator`` tracks, independently, the highest token it has
ever accepted for a given ``agent_id``, and rejects any write presenting a
lower one. This closes the gap a TTL lease alone cannot: a workflow whose
lease was reclaimed while its worker was unavailable has no way to know
that happened, so its resumed, stale-token writes must be refused at the
point of mutation rather than merely discouraged by a renewed-but-still-racy
lock.

Deliberately has no dependency on ``agent_lock.py`` — that module is the
orchestration-lock concern (who owns ``agent_id`` right now); this module is
the resource-store concern (is this write allowed to proceed). The two are
connected only by both handling the same integer, minted solely by
``AgentLockStore.acquire()``.
"""

from __future__ import annotations

from typing import Optional


class StaleFencingTokenError(RuntimeError):
    """A caller's fencing token is older than one this resource already saw.

    Attributes:
        agent_id: The agent_id the rejected write targeted.
        resource: Short label for the rejecting store, e.g.
            ``"environment_store"`` or ``"provisioner_state:docker_provisioner"``.
        provided_token: The token the caller presented.
        current_token: The highest token this resource had already recorded
            for ``(resource, agent_id)``.
    """

    def __init__(
        self, agent_id: str, resource: str, provided_token: int, current_token: int
    ) -> None:
        self.agent_id = agent_id
        self.resource = resource
        self.provided_token = provided_token
        self.current_token = current_token
        super().__init__(
            f"stale fencing token for agent_id={agent_id!r} resource={resource!r}: "
            f"provided={provided_token} current={current_token}"
        )


def check_fencing_token(
    *,
    agent_id: str,
    resource: str,
    provided_token: int,
    current_token: Optional[int],
) -> None:
    """Reject ``provided_token`` if it is older than ``current_token``.

    Preconditions:
        * ``provided_token`` is an ``int``.
        * ``current_token`` is the highest token this resource has recorded
          for ``(resource, agent_id)`` so far, or ``None`` if this resource
          has never recorded a token for that key yet (bootstrap).
    Postconditions:
        * Returns ``None`` (accepted) when ``current_token`` is ``None`` or
          ``provided_token >= current_token``. The "``>=``", not "``>``", is
          required: concurrent tool-provisioning activities fan out from a
          single acquired/renewed token and all present the *same* value: a
          strict "``>``" would wrongly reject the 2nd..Nth concurrent writer.
        * Raises :class:`StaleFencingTokenError` otherwise.
        * Does not itself persist anything — callers persist
          ``provided_token`` as their own new high-water mark as part of
          their own write.
    """
    assert isinstance(provided_token, int), "provided_token must be an int"
    if current_token is not None and provided_token < current_token:
        raise StaleFencingTokenError(agent_id, resource, provided_token, current_token)
