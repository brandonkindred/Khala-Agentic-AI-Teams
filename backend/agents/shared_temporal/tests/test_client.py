"""Tests for shared_temporal.client's Temporal connection wiring.

Guards that the client and worker actually agree on the payload codec: the
worker is built from the exact `Client` `connect_temporal_client()` returns
(see `shared_temporal/worker.py`), so a codec mismatch between them would
silently break decoding rather than raise — this pins the `data_converter`
argument passed to `Client.connect`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared_temporal import client as client_mod
from shared_temporal.codec import GzipPayloadCodec, min_compress_bytes


@pytest.mark.asyncio
async def test_connect_temporal_client_passes_shared_data_converter(monkeypatch):
    """connect_temporal_client() must call Client.connect with the same
    DataConverter shared_temporal.codec.build_data_converter() would build, so
    the client (used to start/signal workflows) and the worker (constructed
    from that same client) decode payloads identically."""
    monkeypatch.setenv("TEMPORAL_ADDRESS", "localhost:7233")
    monkeypatch.delenv("TEMPORAL_PAYLOAD_COMPRESSION", raising=False)
    monkeypatch.delenv("TEMPORAL_PAYLOAD_COMPRESSION_MIN_BYTES", raising=False)
    fake_client = object()

    with patch(
        "temporalio.client.Client.connect", new=AsyncMock(return_value=fake_client)
    ) as mock_connect:
        result = await client_mod.connect_temporal_client()

    assert result is fake_client
    mock_connect.assert_awaited_once()
    args, kwargs = mock_connect.call_args
    assert args == ("localhost:7233",)
    assert kwargs["namespace"] == "default"
    converter = kwargs["data_converter"]
    assert isinstance(converter.payload_codec, GzipPayloadCodec)
    assert converter.payload_codec._min_size_bytes == min_compress_bytes()
    # Encoding defaults off (rollout safety); decode is unconditional either way.
    assert converter.payload_codec._encode_enabled is False


@pytest.mark.asyncio
async def test_connect_temporal_client_keeps_codec_installed_when_encoding_disabled(monkeypatch):
    """Explicitly disabling compression must still install the codec (for
    decode) — only its encode side is gated. A bare `payload_codec is None`
    would mean this process can't read a payload another, encoding-enabled
    process already compressed."""
    monkeypatch.setenv("TEMPORAL_ADDRESS", "localhost:7233")
    monkeypatch.setenv("TEMPORAL_PAYLOAD_COMPRESSION", "false")
    fake_client = object()

    with patch(
        "temporalio.client.Client.connect", new=AsyncMock(return_value=fake_client)
    ) as mock_connect:
        await client_mod.connect_temporal_client()

    _, kwargs = mock_connect.call_args
    codec = kwargs["data_converter"].payload_codec
    assert isinstance(codec, GzipPayloadCodec)
    assert codec._encode_enabled is False


@pytest.mark.asyncio
async def test_connect_temporal_client_honors_compression_enabled(monkeypatch):
    monkeypatch.setenv("TEMPORAL_ADDRESS", "localhost:7233")
    monkeypatch.setenv("TEMPORAL_PAYLOAD_COMPRESSION", "true")
    fake_client = object()

    with patch(
        "temporalio.client.Client.connect", new=AsyncMock(return_value=fake_client)
    ) as mock_connect:
        await client_mod.connect_temporal_client()

    _, kwargs = mock_connect.call_args
    assert kwargs["data_converter"].payload_codec._encode_enabled is True


@pytest.mark.asyncio
async def test_connect_temporal_client_returns_none_when_disabled(monkeypatch):
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    assert await client_mod.connect_temporal_client() is None
