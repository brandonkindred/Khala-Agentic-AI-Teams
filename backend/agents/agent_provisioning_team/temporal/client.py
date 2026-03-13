"""Temporal client for the Agent Provisioning team."""

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
    return os.getenv("TEMPORAL_NAMESPACE", "default").strip()


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
    from temporalio.client import Client
    address = get_temporal_address()
    if not address:
        return None
    namespace = get_temporal_namespace()
    try:
        client = await Client.connect(address, namespace=namespace)
        logger.info("Agent Provisioning Temporal client connected to %s namespace %s", address, namespace)
        return client
    except Exception as e:
        logger.exception("Agent Provisioning Temporal client connection failed: %s", e)
        raise
