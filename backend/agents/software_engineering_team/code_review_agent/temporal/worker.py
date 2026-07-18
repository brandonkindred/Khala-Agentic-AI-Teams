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

from shared_env_config import env_int
from shared_temporal import start_team_worker

from . import ACTIVITIES, WORKFLOWS
from .config import (
    TASK_QUEUE,
    code_review_temporal_enabled,
    resolve_code_review_temporal_address,
)

logger = logging.getLogger(__name__)

# Default concurrent-activity ceiling for the code review worker. The shared
# framework default (``start_team_worker``'s own default, 4) is sized for
# teams with narrow, fixed-width activity fan-out; code review's map phase
# instead fans out one activity per review chunk
# (``temporal/workflows.py``'s ``asyncio.gather`` over ``review_chunk_activity``),
# and a large PR can produce dozens of chunks (``chunking.build_review_chunks``
# has no upper bound on chunk count) — at 4 concurrent slots that is many
# sequential rounds, each potentially bounded only by a single chunk's
# multi-hour worst-case retry budget (``temporal/workflows.py``'s
# ``_LLM_RETRY``: 3 attempts x up to 1h ``start_to_close_timeout`` + backoff).
# 8 mirrors ``sales_team``'s ``SALES_TEMPORAL_MAX_CONCURRENT_ACTIVITIES`` and
# ``investment_team``'s ``INVESTMENT_MAX_CONCURRENT_ACTIVITIES`` defaults,
# both raised from the same 4-slot shared default for the identical "narrow
# default starves a wide fan-out" reason. This is independent of
# ``CODE_REVIEW_MAP_PARALLELISM`` (also defaulting to 4) — that knob governs
# only the in-process thread-mode fallback, not this Temporal-mode fan-out
# (see its entry in docs/ENV_VARS.md).
_DEFAULT_MAX_CONCURRENT_ACTIVITIES = 8


def _max_concurrent_activities() -> int:
    """Resolve the code review worker's concurrent-activity ceiling.

    Preconditions:
        - none (environment may be unset or garbage).
    Postconditions:
        - Returns ``CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES`` when it parses to
          a positive int, else :data:`_DEFAULT_MAX_CONCURRENT_ACTIVITIES`
          (unset, garbage, or <= 0 all fall back to the default), via the
          shared ``env_int`` parser (which warns on a set-but-unparseable
          value). Never raises.
    """
    return env_int(
        "CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES",
        _DEFAULT_MAX_CONCURRENT_ACTIVITIES,
        floor=1,
    )


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
        max_concurrent_activities=_max_concurrent_activities(),
    )
