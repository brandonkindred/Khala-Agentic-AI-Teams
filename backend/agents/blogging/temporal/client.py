"""Blogging Temporal client — thin re-export of ``shared_temporal.client``.

The Temporal connection helpers now live in ``shared_temporal.client`` so every
team shares one cached client and event loop (one source of truth). This module
stays as a compatibility shim for existing ``blogging.temporal.client`` imports.
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
