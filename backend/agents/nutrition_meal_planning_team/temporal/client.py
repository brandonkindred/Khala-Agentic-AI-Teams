"""Nutrition & Meal Planning Temporal client — thin re-export of ``shared_temporal.client``.

The Temporal connection helpers now live in ``shared_temporal.client`` so every
team shares one cached client and event loop (one source of truth) — and, in
particular, one ``DataConverter`` with the shared gzip payload codec
(``shared_temporal.codec``). This team's own ``worker.py``/``start_workflow.py``
import directly from ``shared_temporal`` rather than through this module; it
stays as a compatibility shim in case other code imports
``nutrition_meal_planning_team.temporal.client`` directly — matching the
equivalent shim every other migrated team keeps for the same reason.
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
