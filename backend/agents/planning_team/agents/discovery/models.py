"""Boundary contracts for the Discovery agent (§1 typed Input/Output)."""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel

from planning_team.models import ClientContext


class DiscoveryInput(BaseModel):
    """Upstream material for discovery.

    ``client_context`` carries the raw upstream value (a ``ClientContext``, a plain
    dict, or ``None``) exactly as it crosses the phase seam; the agent normalizes it
    via the shared ``as_client_context`` so coercion behaviour is preserved.
    """

    model_config = {"arbitrary_types_allowed": True}

    client_context: Any = None
    initial_brief: Optional[str] = None
    spec_content: Optional[str] = None


class DiscoveryOutput(BaseModel):
    """Structured discovery result.

    Postconditions:
        - ``client_context`` is the prior context overlaid with the six discovery
          fields (list fields normalized to ``list[str]``, assumptions accumulated).
        - ``discovery`` is the raw reduced LLM dict (the ``discovery`` artifact).
    """

    model_config = {"arbitrary_types_allowed": True}

    client_context: ClientContext
    discovery: Dict[str, Any]
