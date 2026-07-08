"""Tests for the shared Temporal payload compression codec.

Guards the fix for the coding team's ``PayloadSizeWarning`` (``TMPRL1103``):
a large code-review payload must compress below Temporal's 512 KiB warning
threshold, round-trip losslessly, and stay a no-op for small payloads.
"""

from __future__ import annotations

import gzip
import os

import pytest
from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter, PayloadCodec

from shared_temporal.codec import (
    DEFAULT_MIN_COMPRESS_BYTES,
    GzipPayloadCodec,
    build_data_converter,
    compression_enabled,
    min_compress_bytes,
)


def _payload(data: bytes, metadata: dict[bytes, bytes] | None = None) -> Payload:
    return Payload(metadata=metadata or {b"encoding": b"json/plain"}, data=data)


@pytest.mark.asyncio
async def test_large_payload_round_trips_and_shrinks_below_temporal_warning_threshold():
    """A ~565 KiB code-review-shaped payload (the reported failure's size) must
    compress under Temporal's 512 KiB (524288 byte) warning limit and decode
    back to the exact original bytes."""
    # Repetitive source-like text compresses well; this mirrors the map-reduce
    # code review chunks that triggered the original warning.
    line = b"    def method(self, arg: int) -> None:\n        self.value = arg\n"
    data = line * 9000  # comfortably over 512 KiB before compression
    assert len(data) > 524288
    original = _payload(data)

    codec = GzipPayloadCodec(min_size_bytes=1024)
    encoded = await codec.encode([original])
    assert len(encoded) == 1
    assert encoded[0].metadata[b"encoding"] == b"binary/gzip"
    assert len(encoded[0].SerializeToString()) < 524288

    decoded = await codec.decode(encoded)
    assert decoded == [original]


@pytest.mark.asyncio
async def test_small_payload_passes_through_uncompressed():
    """Below the size floor, encode() must return the payload unchanged (no
    metadata added, no gzip overhead) and decode() must be a no-op for it."""
    original = _payload(b"tiny")
    codec = GzipPayloadCodec(min_size_bytes=DEFAULT_MIN_COMPRESS_BYTES)

    encoded = await codec.encode([original])
    assert encoded == [original]

    decoded = await codec.decode(encoded)
    assert decoded == [original]


@pytest.mark.asyncio
async def test_encode_passes_through_when_compression_does_not_shrink_payload():
    """High-entropy/already-compressed data above the size floor must not be
    wrapped in gzip if doing so would make the payload larger — this codec is
    installed globally, so it must never push a payload that already fit
    Temporal's limits over them."""
    codec = GzipPayloadCodec(min_size_bytes=1024)
    # os.urandom output is incompressible; gzip's header/table overhead makes
    # the "compressed" result larger than the input.
    original = _payload(os.urandom(4096))

    encoded = await codec.encode([original])
    assert encoded == [original]

    decoded = await codec.decode(encoded)
    assert decoded == [original]


@pytest.mark.asyncio
async def test_decode_passes_through_payload_not_tagged_by_this_codec():
    """A payload lacking the gzip encoding marker (e.g. written before
    compression was enabled) must decode to itself unchanged, never raise."""
    codec = GzipPayloadCodec()
    plain = _payload(b"some plain, never-compressed bytes")

    decoded = await codec.decode([plain])
    assert decoded == [plain]


@pytest.mark.asyncio
async def test_encode_is_lossless_for_binary_data_and_empty_payloads():
    codec = GzipPayloadCodec(min_size_bytes=0)
    empty = _payload(b"")
    binary = _payload(bytes(range(256)) * 10)

    for original in (empty, binary):
        encoded = await codec.encode([original])
        decoded = await codec.decode(encoded)
        assert decoded == [original]


def test_gzip_payload_codec_is_a_real_payload_codec():
    assert isinstance(GzipPayloadCodec(), PayloadCodec)


def test_compression_enabled_defaults_true(monkeypatch):
    monkeypatch.delenv("TEMPORAL_PAYLOAD_COMPRESSION", raising=False)
    assert compression_enabled() is True


def test_compression_enabled_respects_false_override(monkeypatch):
    monkeypatch.setenv("TEMPORAL_PAYLOAD_COMPRESSION", "false")
    assert compression_enabled() is False


def test_compression_enabled_falls_back_to_default_on_garbage(monkeypatch):
    monkeypatch.setenv("TEMPORAL_PAYLOAD_COMPRESSION", "not-a-bool")
    assert compression_enabled() is True


def test_min_compress_bytes_default(monkeypatch):
    monkeypatch.delenv("TEMPORAL_PAYLOAD_COMPRESSION_MIN_BYTES", raising=False)
    assert min_compress_bytes() == DEFAULT_MIN_COMPRESS_BYTES


def test_min_compress_bytes_clamped_to_zero_floor(monkeypatch):
    monkeypatch.setenv("TEMPORAL_PAYLOAD_COMPRESSION_MIN_BYTES", "-50")
    assert min_compress_bytes() == 0


def test_build_data_converter_returns_default_when_disabled(monkeypatch):
    monkeypatch.setenv("TEMPORAL_PAYLOAD_COMPRESSION", "false")
    converter = build_data_converter()
    assert converter is DataConverter.default
    assert converter.payload_codec is None


def test_build_data_converter_installs_gzip_codec_when_enabled(monkeypatch):
    monkeypatch.setenv("TEMPORAL_PAYLOAD_COMPRESSION", "true")
    monkeypatch.setenv("TEMPORAL_PAYLOAD_COMPRESSION_MIN_BYTES", "2048")
    converter = build_data_converter()
    assert isinstance(converter.payload_codec, GzipPayloadCodec)
    assert converter.payload_codec._min_size_bytes == 2048
    # Everything else about the default converter (payload/failure converter
    # classes) must be preserved — only the codec is added.
    assert converter.payload_converter_class is DataConverter.default.payload_converter_class


@pytest.mark.asyncio
async def test_gzip_compress_and_decompress_are_stdlib_symmetric():
    """Sanity check the codec's on-the-wire bytes really are gzip (so any
    external tooling/codec server can decode them with plain ``gzip``)."""
    codec = GzipPayloadCodec(min_size_bytes=0)
    original = _payload(b"x" * 5000)

    encoded = await codec.encode([original])
    raw_decompressed = gzip.decompress(encoded[0].data)
    reconstructed = Payload()
    reconstructed.ParseFromString(raw_decompressed)
    assert reconstructed == original
