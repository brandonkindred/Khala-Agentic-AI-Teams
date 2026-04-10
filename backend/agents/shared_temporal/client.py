"""Shared Temporal client.

Single cached ``Client`` + event loop used by every team worker and every
sync HTTP handler that needs to start/signal workflows via
``run_coroutine_threadsafe``. Lifted from
``software_engineering_team/temporal/client.py`` so there is one source of
truth.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from temporalio.client import Client

logger = logging.getLogger(__name__)

_client: Optional["Client"] = None
_loop: Optional[asyncio.AbstractEventLoop] = None


def get_temporal_address() -> Optional[str]:
    return os.getenv("TEMPORAL_ADDRESS", "").strip() or None


def get_temporal_namespace() -> str:
    return os.getenv("TEMPORAL_NAMESPACE", "default").strip() or "default"


def get_default_task_queue() -> str:
    return os.getenv("TEMPORAL_TASK_QUEUE", "khala").strip() or "khala"


def is_temporal_enabled() -> bool:
    return get_temporal_address() is not None


def get_temporal_client() -> Optional["Client"]:
    return _client


def set_temporal_client(client: Optional["Client"]) -> None:
    global _client
    _client = client


def get_temporal_loop() -> Optional[asyncio.AbstractEventLoop]:
    return _loop


def set_temporal_loop(loop: Optional[asyncio.AbstractEventLoop]) -> None:
    global _loop
    _loop = loop


async def connect_temporal_client() -> Optional["Client"]:
    """Connect to Temporal using env vars. Returns None if not configured."""
    from temporalio.client import Client

    address = get_temporal_address()
    if not address:
        return None
    namespace = get_temporal_namespace()
    try:
        client = await Client.connect(address, namespace=namespace)
        logger.info("Temporal client connected to %s namespace %s", address, namespace)
        return client
    except Exception as e:
        logger.exception("Temporal client connection failed: %s", e)
        raise
