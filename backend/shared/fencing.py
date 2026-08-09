"""Fencing-token enforcement shared by any resource-mutating store.

A fencing token is a per-resource-key monotonic counter minted whenever a
new "incarnation" of some ownership/execution takes over from a prior one
(a reclaimed lease, a restarted workflow, ...). Every store that mutates a
key on behalf of that ownership tracks, independently, the highest token it
has ever accepted for that key, and rejects any write presenting a lower
one. This closes the gap a lease or termination confirmation alone cannot:
the *prior* incarnation has no way to know it was superseded, so its
resumed, stale-token writes must be refused at the point of mutation rather
than merely discouraged by a renewed-but-still-racy lock or a "the old
workflow was terminated" check that can't stop an already-dispatched
activity from finishing anyway.

Deliberately has no dependency on any particular lock/lease implementation
or minting source — this module is the resource-store concern (is this
write allowed to proceed), not the ownership/orchestration concern (who
owns the key right now). The two are connected only by both handling the
same integer.

Promoted here from ``agent_provisioning_team/shared/fencing.py`` (which now
re-exports these names) after Strategy Lab's restart-generation fencing
needed the identical primitive outside that team.
"""

from __future__ import annotations

from typing import Optional


class StaleFencingTokenError(RuntimeError):
    """A caller's fencing token is older than one this resource already saw.

    Attributes:
        agent_id: The resource key the rejected write targeted (e.g. an
            agent id, a run id).
        resource: Short label for the rejecting store, e.g.
            ``"environment_store"`` or ``"strategy_lab_run"``.
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
        * ``provided_token`` is an ``int``. Enforced with an explicit
          ``TypeError`` rather than ``assert``: assertions are stripped
          under Python's ``-O`` flag, which would otherwise silently disable
          this check in an optimized deployment and let a non-int token
          reach the comparison below, raising an unrelated ``TypeError``
          from the comparison itself instead of a clear precondition
          violation.
        * ``current_token`` is the highest token this resource has recorded
          for ``(resource, agent_id)`` so far, or ``None`` if this resource
          has never recorded a token for that key yet (bootstrap).
    Postconditions:
        * Returns ``None`` (accepted) when ``current_token`` is ``None`` or
          ``provided_token >= current_token``. The "``>=``", not "``>``", is
          required: concurrent writers fanned out from a single
          acquired/renewed token all present the *same* value: a strict
          "``>``" would wrongly reject the 2nd..Nth concurrent writer.
        * Raises :class:`StaleFencingTokenError` when the token comparison
          fails, or ``TypeError`` when ``provided_token`` is not an ``int``.
        * Does not itself persist anything — callers persist
          ``provided_token`` as their own new high-water mark as part of
          their own write.
    """
    if not isinstance(provided_token, int):
        raise TypeError(f"provided_token must be an int, got {type(provided_token).__name__}")
    if current_token is not None and provided_token < current_token:
        raise StaleFencingTokenError(agent_id, resource, provided_token, current_token)
