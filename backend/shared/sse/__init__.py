"""Shared Server-Sent-Events helpers for per-job streaming endpoints.

Re-exports the SSE framing helper and the per-job stream generators from
:mod:`shared_sse.stream`. See that module's docstring for the streaming contract
and the sync/async split.
"""

from __future__ import annotations

from shared_sse.stream import (
    SSE_KEEPALIVE,
    sse_job_stream_async,
    sse_job_stream_sync,
    sse_line,
)

__all__ = [
    "SSE_KEEPALIVE",
    "sse_line",
    "sse_job_stream_sync",
    "sse_job_stream_async",
]
