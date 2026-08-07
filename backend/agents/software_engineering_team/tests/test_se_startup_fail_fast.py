"""SE ASGI startup must fail fast when Temporal is missing or unreachable."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from software_engineering_team.api import lifecycle


def test_assert_temporal_ready_raises_when_disabled(monkeypatch):
    monkeypatch.setattr(
        "shared.temporal.client.is_temporal_enabled",
        lambda: False,
    )
    connect = AsyncMock()
    monkeypatch.setattr(
        "shared.temporal.client.connect_temporal_client",
        connect,
    )

    with pytest.raises(RuntimeError, match="TEMPORAL_ADDRESS"):
        asyncio.run(lifecycle._assert_temporal_ready())

    connect.assert_not_awaited()


def test_assert_temporal_ready_propagates_connect_failure(monkeypatch):
    monkeypatch.setattr(
        "shared.temporal.client.is_temporal_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "shared.temporal.client.connect_temporal_client",
        AsyncMock(side_effect=OSError("connection refused")),
    )

    with pytest.raises(OSError, match="connection refused"):
        asyncio.run(lifecycle._assert_temporal_ready())


def test_assert_temporal_ready_succeeds_when_connect_ok(monkeypatch):
    monkeypatch.setattr(
        "shared.temporal.client.is_temporal_enabled",
        lambda: True,
    )
    client = object()
    monkeypatch.setattr(
        "shared.temporal.client.connect_temporal_client",
        AsyncMock(return_value=client),
    )

    asyncio.run(lifecycle._assert_temporal_ready())
