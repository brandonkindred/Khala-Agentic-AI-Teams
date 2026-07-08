"""Payload compression for the shared Temporal client.

The map-reduce code review workflow (and any other team) can legitimately need
to move hundreds of kilobytes of source code through a single Temporal
activity/workflow payload — the code under review is deliberately never
truncated (see ``code_review_agent/temporal/activities.py``), so a large
multi-file diff can push one payload past Temporal's 512 KiB
``PayloadSizeWarning`` threshold (``TMPRL1103``) even though the underlying
gRPC message stays well under its own 4 MiB cap. Source/JSON text compresses
well, so a transparent gzip codec on the shared ``DataConverter`` shrinks the
bytes that actually cross the wire and land in workflow history — without
touching chunk sizing, review fidelity, or the durability of any workflow.

Every payload keeps the plain (uncompressed) proto encoding below
``min_size_bytes``: gzip's own header/footer overhead makes compression a net
loss for small payloads, and there is nothing to gain size-warning-wise from
compressing them anyway.
"""

from __future__ import annotations

import dataclasses
import gzip
import logging
from typing import List, Sequence

# This module is only ever imported lazily, from inside
# ``shared_temporal.client``'s connection functions (never from
# ``shared_temporal/__init__.py``), so importing ``temporalio`` at module
# scope here does not force it onto every package import the way it would
# from the package root.
from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter, PayloadCodec

from shared_env_config import env_bool, env_int

logger = logging.getLogger(__name__)

# Payload metadata key/value Temporal codecs conventionally use to record their
# encoding, so a decode() can tell a codec-produced payload from a passthrough
# one (see the temporalio-samples-python encryption/compression samples).
_ENCODING_METADATA_KEY = b"encoding"
_GZIP_ENCODING = b"binary/gzip"

DEFAULT_MIN_COMPRESS_BYTES = 1024


def compression_enabled() -> bool:
    """Whether the shared Temporal client should compress payloads.

    Postconditions:
        - Returns the parsed ``TEMPORAL_PAYLOAD_COMPRESSION`` env var, default
          True (garbage/unset falls back to enabled).
    """
    return env_bool("TEMPORAL_PAYLOAD_COMPRESSION", True)


def min_compress_bytes() -> int:
    """Smallest serialized payload size (bytes) worth gzip-compressing.

    Postconditions:
        - Returns ``TEMPORAL_PAYLOAD_COMPRESSION_MIN_BYTES``, clamped to
          ``>= 0``; unset/garbage falls back to ``DEFAULT_MIN_COMPRESS_BYTES``.
    """
    return env_int("TEMPORAL_PAYLOAD_COMPRESSION_MIN_BYTES", DEFAULT_MIN_COMPRESS_BYTES, floor=0)


class GzipPayloadCodec(PayloadCodec):
    """Transparently gzip-compresses proto-serialized payloads above a size floor.

    Invariants:
        - ``decode`` is the exact inverse of ``encode`` for every payload
          ``encode`` produced, and a pass-through (unmarked) payload decodes to
          itself unchanged — so a payload written before compression was
          enabled (or one below the size floor) still decodes correctly.
    """

    def __init__(self, min_size_bytes: int = DEFAULT_MIN_COMPRESS_BYTES) -> None:
        assert min_size_bytes >= 0, "min_size_bytes must be >= 0"
        self._min_size_bytes = min_size_bytes

    async def encode(self, payloads: Sequence[Payload]) -> List[Payload]:
        """Gzip-compress each payload at or above the size floor.

        Postconditions:
            - A payload whose serialized size is ``< min_size_bytes`` is
              returned unchanged (no metadata added).
            - Otherwise gzip-compresses the payload's serialized bytes; when the
              compressed, tagged result is not actually smaller than the
              original (already-compressed or high-entropy binary data, where
              gzip's own header/table overhead can lose), the original payload
              is returned unchanged instead — this codec must never make a
              payload larger, which would defeat its purpose of staying under
              Temporal's payload size limits.
        """
        result: List[Payload] = []
        for payload in payloads:
            serialized = payload.SerializeToString()
            if len(serialized) < self._min_size_bytes:
                result.append(payload)
                continue
            candidate = Payload(
                metadata={_ENCODING_METADATA_KEY: _GZIP_ENCODING},
                data=gzip.compress(serialized),
            )
            if len(candidate.SerializeToString()) >= len(serialized):
                result.append(payload)
                continue
            result.append(candidate)
        return result

    async def decode(self, payloads: Sequence[Payload]) -> List[Payload]:
        """Inverse of :meth:`encode`.

        Postconditions:
            - A payload not carrying this codec's ``encoding`` metadata is
              returned unchanged (covers payloads below the compression floor
              and payloads written before compression was enabled).
            - Otherwise returns the original ``Payload`` reconstructed from the
              decompressed bytes.
        """
        result: List[Payload] = []
        for payload in payloads:
            if payload.metadata.get(_ENCODING_METADATA_KEY) != _GZIP_ENCODING:
                result.append(payload)
                continue
            decoded = Payload()
            decoded.ParseFromString(gzip.decompress(payload.data))
            result.append(decoded)
        return result


def build_data_converter() -> DataConverter:
    """Build the ``DataConverter`` the shared Temporal client/worker should use.

    Postconditions:
        - Returns the plain default converter when ``TEMPORAL_PAYLOAD_COMPRESSION``
          resolves False.
        - Otherwise returns the default converter with a ``GzipPayloadCodec``
          installed (sized by ``TEMPORAL_PAYLOAD_COMPRESSION_MIN_BYTES``) so
          large activity/workflow payloads compress transparently for every
          team on the shared client — client and worker must agree on this, and
          both resolve it through this one function.
    """
    if not compression_enabled():
        return DataConverter.default
    return dataclasses.replace(
        DataConverter.default,
        payload_codec=GzipPayloadCodec(min_compress_bytes()),
    )
