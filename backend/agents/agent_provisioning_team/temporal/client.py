"""Agent Provisioning Temporal client — thin re-export of ``shared_temporal.client``.

The Temporal connection helpers now live in ``shared_temporal.client`` so every
team shares one cached client and event loop (one source of truth) — and, in
particular, one ``DataConverter`` with the shared gzip payload codec
(``shared_temporal.codec``). This module stays as a compatibility shim for
existing ``agent_provisioning_team.temporal.client`` imports.
"""

from __future__ import annotations

import os

from shared_temporal.client import (  # noqa: F401
    connect_temporal_client,
    get_temporal_address,
    get_temporal_client,
    get_temporal_loop,
    get_temporal_namespace,
    is_temporal_enabled,
    set_temporal_client,
    set_temporal_loop,
)


def provision_thread_fallback_enabled() -> bool:
    """True when ``PROVISION_THREAD_FALLBACK`` forces the in-process path for
    the whole Agent Provisioning team, even when Temporal is otherwise enabled.

    The single source of truth for this escape hatch, so provisioning
    (``api/main.py``), deprovision (``api/main.py``), and the sandbox lifecycle
    (``temporal/sandbox_dispatch.py``) can never independently drift on which
    accepted spellings disable Temporal — a desync there would silently break
    the "loop affinity — never half-migrated" invariant the sandbox lifecycle
    depends on (see ``sandbox/lifecycle.py``).

    Postconditions:
        * Returns ``False`` (never raises) when the env var is unset/blank.
    """
    return os.getenv("PROVISION_THREAD_FALLBACK", "").strip().lower() in ("1", "true", "yes")
