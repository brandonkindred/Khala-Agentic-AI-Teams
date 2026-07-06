"""Temporal client for the Planning team."""

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
    """Return the Temporal server address from ``TEMPORAL_ADDRESS``, or None if unset."""
    return os.getenv("TEMPORAL_ADDRESS", "").strip() or None


def get_temporal_namespace() -> str:
    """Return the Temporal namespace from ``TEMPORAL_NAMESPACE`` (default ``"default"``)."""
    return os.getenv("TEMPORAL_NAMESPACE", "default").strip()


def is_temporal_enabled() -> bool:
    """Return True when a Temporal address is configured (Temporal mode vs. thread mode)."""
    return get_temporal_address() is not None


def get_temporal_client() -> Optional["Client"]:
    """Return the process-wide connected Temporal client, or None if not yet connected."""
    return _client


def set_temporal_client(client: Optional["Client"]) -> None:
    """Store the process-wide Temporal client (called once by the worker startup)."""
    global _client
    _client = client


def get_temporal_loop() -> Optional[asyncio.AbstractEventLoop]:
    """Return the event loop the Temporal worker/client is running on, or None if unset."""
    return _loop


def set_temporal_loop(loop: Optional[asyncio.AbstractEventLoop]) -> None:
    """Store the event loop the Temporal worker/client runs on (called once by worker startup)."""
    global _loop
    _loop = loop


async def connect_temporal_client() -> Optional["Client"]:
    """Connect to the Temporal server at ``get_temporal_address()``.

    Returns:
        A connected Client, or None if no Temporal address is configured.

    Raises:
        Exception: Whatever the underlying `Client.connect` raises on a
            connection failure (logged before re-raising).
    """
    from temporalio.client import Client

    address = get_temporal_address()
    if not address:
        return None
    namespace = get_temporal_namespace()
    try:
        client = await Client.connect(address, namespace=namespace)
        logger.info("Planning Temporal client connected to %s namespace %s", address, namespace)
        return client
    except Exception as e:
        logger.exception("Planning Temporal client connection failed: %s", e)
        raise
