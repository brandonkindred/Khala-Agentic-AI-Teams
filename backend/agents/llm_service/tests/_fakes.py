"""Shared test double for draining Strands async streams — used across
``test_strands_adapter.py`` and ``test_cache_breakpoint_e2e.py`` so each
doesn't maintain its own copy.

The Claude/Anthropic-SDK fakes (``_FakeStreamCtx``, ``_text_message``,
``_build_claude_client``, ``_make_claude_client``, etc.) live in the
top-level ``llm_client_fakes`` module instead, on the standard agents
pythonpath, so other teams' tests can import them without reaching into this
package's private ``tests/`` internals — see that module's docstring.

Leading underscore keeps this out of pytest's test collection.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

__all__ = ["_drain"]


def _drain(gen) -> List[Dict[str, Any]]:
    """Drain a Strands async stream into a list for easy assertions."""

    async def _run() -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        async for event in gen:
            out.append(event)
        return out

    return asyncio.run(_run())
