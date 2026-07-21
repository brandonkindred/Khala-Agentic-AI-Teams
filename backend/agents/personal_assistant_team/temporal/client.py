"""Personal assistant Temporal client — thin re-export of ``shared.temporal.client``.

The Temporal connection helpers now live in ``shared.temporal.client`` so every
team shares one cached client and event loop (one source of truth) — and, in
particular, one ``DataConverter`` with the shared gzip payload codec
(``shared.temporal.codec``). This module stays as a compatibility shim for
existing ``personal_assistant_team.temporal.client`` imports.
"""

from __future__ import annotations

from shared.temporal.client import (  # noqa: F401
    connect_temporal_client,
    get_temporal_address,
    get_temporal_client,
    get_temporal_loop,
    get_temporal_namespace,
    is_temporal_enabled,
    set_temporal_client,
    set_temporal_loop,
)
