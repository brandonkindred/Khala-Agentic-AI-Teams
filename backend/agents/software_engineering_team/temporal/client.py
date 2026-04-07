"""SE Temporal client — thin re-export of ``shared_temporal.client``.

Historically the SE team owned the canonical Temporal client module. It now
lives in ``shared_temporal`` so every team shares one cached client and
event loop. This module stays as a compatibility shim for existing imports.
"""

from __future__ import annotations

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
