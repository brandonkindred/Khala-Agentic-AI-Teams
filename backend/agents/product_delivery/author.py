"""Stable author handle for product_delivery rows.

Re-exports :func:`agent_console.author.resolve_author` so every persisted
row in this team carries the same handle the Agent Console writes to its
own tables (see ``agent_console_runs.author``). When auth lands, a
single migration can map both teams' rows to user ids in lockstep.
"""

from __future__ import annotations

from agent_console.author import resolve_author

__all__ = ["resolve_author"]
