"""Temporal worker bootstrap for the code review agent.

Exposes a no-arg ``start_code_review_temporal_worker_thread`` that mirrors every
team's worker hook (e.g. ``market_research_team.temporal.worker``): idempotent,
returns whether a worker is running. It is invoked lazily by
``CodeReviewAgent.run`` on the first Temporal-mode review, and can also be wired
via the generic ``TEAM_TEMPORAL_WORKER_MODULE`` / ``TEAM_TEMPORAL_WORKER_FUNC``
entrypoint.

Because the code review agent runs Temporal by default (see :mod:`.config`), when
the process has no ``TEMPORAL_ADDRESS`` set at all this boot points the shared
Temporal client at the default (the app's ``temporal:7233`` container) so the
shared client can connect. In the deployed stack ``TEMPORAL_ADDRESS`` is already
set, so that branch is inert; it only takes effect for an unconfigured run that
has nonetheless opted into (defaulted to) Temporal. An operator's explicit
``TEMPORAL_ADDRESS`` is never overwritten — the shared client connects to exactly
that address, so the override is honored end to end.
"""

from __future__ import annotations

import logging
import os

from shared_temporal import start_team_worker

from . import ACTIVITIES, WORKFLOWS
from .config import (
    TASK_QUEUE,
    code_review_temporal_enabled,
    resolve_code_review_temporal_address,
)

logger = logging.getLogger(__name__)


def start_code_review_temporal_worker_thread() -> bool:
    """Start the code review Temporal worker (no-op when disabled).

    Postconditions:
        - Returns ``False`` when code-review Temporal is disabled (sentinel /
          ``dummy`` / under pytest) and starts nothing.
        - Otherwise ensures the shared client has an address to connect to
          (defaulting ``TEMPORAL_ADDRESS`` to the deployed container only when it
          is unset — never overwriting an operator's value) and starts the worker
          on the ``code_review-queue``. Idempotent per team.
    """
    if not code_review_temporal_enabled():
        return False

    address = resolve_code_review_temporal_address()
    if address and not os.environ.get("TEMPORAL_ADDRESS", "").strip():
        # Point the shared Temporal client at the resolved default so it can
        # connect; only when the operator left TEMPORAL_ADDRESS unset.
        os.environ["TEMPORAL_ADDRESS"] = address
        logger.info("Code review Temporal defaulting shared client to %s", address)

    return start_team_worker(
        "code_review",
        WORKFLOWS,
        ACTIVITIES,
        task_queue=TASK_QUEUE,
    )
