"""
Temporal client for the software engineering team.

When TEMPORAL_ADDRESS is set, the unified API (or SE standalone) connects at startup
and stores the client here so the SE API can start workflows from sync endpoints
via run_coroutine_threadsafe.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from temporalio.client import Client

logger = logging.getLogger(__name__)

# Set by unified API (or SE standalone) lifespan after connect_temporal_client()
_client: Optional["Client"] = None
_loop: Optional[asyncio.AbstractEventLoop] = None


def get_temporal_address() -> Optional[str]:
    """Return TEMPORAL_ADDRESS if set and non-empty, else None."""
    return os.getenv("TEMPORAL_ADDRESS", "").strip() or None


def get_temporal_namespace() -> str:
    """Return TEMPORAL_NAMESPACE (default 'default')."""
    return os.getenv("TEMPORAL_NAMESPACE", "default").strip()


def is_temporal_enabled() -> bool:
    """True when Temporal should be used (TEMPORAL_ADDRESS set)."""
    return get_temporal_address() is not None


def get_temporal_client() -> Optional["Client"]:
    """Return the cached Temporal client, or None if not connected."""
    return _client


def set_temporal_client(client: Optional["Client"]) -> None:
    """Set the cached Temporal client (called from lifespan after connect)."""
    global _client
    _client = client


def get_temporal_loop() -> Optional[asyncio.AbstractEventLoop]:
    """Return the event loop where the Temporal client was created."""
    return _loop


def set_temporal_loop(loop: Optional[asyncio.AbstractEventLoop]) -> None:
    """Set the event loop for the Temporal client (called from lifespan)."""
    global _loop
    _loop = loop


async def connect_temporal_client() -> Optional["Client"]:
    """
    Connect to Temporal using TEMPORAL_ADDRESS and TEMPORAL_NAMESPACE.
    Returns the Client if TEMPORAL_ADDRESS is set and connection succeeds; None otherwise.
    Raises on connection error when TEMPORAL_ADDRESS is set.
    """
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
