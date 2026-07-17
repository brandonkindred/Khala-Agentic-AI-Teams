"""Boundary contracts for the Requirements agent (§1 typed Input/Output).

Reuses the shared ``OpenQuestion`` domain model from ``planning_team.models``.
"""

from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field

from planning_team.models import OpenQuestion


class RequirementsInput(BaseModel):
    """Upstream material for requirements elicitation.

    ``client_context`` carries the raw upstream value (``ClientContext``, dict, or
    ``None``); the agent normalizes it via the shared ``as_client_context``.
    """

    model_config = {"arbitrary_types_allowed": True}

    client_context: Any = None
    initial_brief: Optional[str] = None
    spec_content: Optional[str] = None


class RequirementsOutput(BaseModel):
    """Structured open questions for the client PO to answer.

    Postconditions:
        - ``open_questions`` is non-empty: when the LLM produces no valid questions
          the deterministic default set (RPO/RTO + deployment) is substituted.
    """

    open_questions: List[OpenQuestion] = Field(default_factory=list)
