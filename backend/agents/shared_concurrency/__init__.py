"""Shared concurrency primitives used across agent teams.

Currently exports :class:`BackgroundHeartbeat`, a single driver for the
"daemon thread runs a callable on an interval until stopped" pattern that
several teams had independently hand-rolled.
"""

from __future__ import annotations

from shared_concurrency.heartbeat import BackgroundHeartbeat

__all__ = ["BackgroundHeartbeat"]
