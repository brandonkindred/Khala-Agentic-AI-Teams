"""Boundary contracts for the Synthesis agent (§1 typed Input/Output)."""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel

from planning_team.models import ClientContext


class SynthesisInput(BaseModel):
    """Optional evidence to fold into the client context.

    ``client_context`` carries the raw upstream value (``ClientContext``, dict, or
    ``None``); the agent normalizes it via the shared ``as_client_context``.
    """

    model_config = {"arbitrary_types_allowed": True}

    client_context: Any = None
    market_research_evidence: Optional[Dict[str, Any]] = None


class SynthesisOutput(BaseModel):
    """Result of folding evidence into the context.

    Fields map to the legacy ``(context_update, artifacts)`` seam:
        - ``evidence`` is always emitted as ``artifacts["evidence"]`` (may be ``None``).
        - ``evidence_attached`` gates ``context_update["market_research_evidence"]``
          (set to the same ``evidence`` value) — true iff evidence was provided.
        - ``client_context`` is emitted as ``context_update["client_context"]`` only
          when the evidence produced a summary/insights update (else ``None``).
    """

    model_config = {"arbitrary_types_allowed": True}

    evidence: Optional[Dict[str, Any]] = None
    evidence_attached: bool = False
    client_context: Optional[ClientContext] = None
