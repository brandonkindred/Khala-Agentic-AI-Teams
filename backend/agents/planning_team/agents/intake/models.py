"""Boundary contracts for the Intake agent (§1 typed Input/Output).

Reuses the shared domain model ``ClientContext`` from ``planning_team.models``
rather than redefining it.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel

from planning_team.models import ClientContext


class IntakeInput(BaseModel):
    """Raw request fields captured at the start of a planning run.

    Invariants:
        - Field names/semantics mirror the ``run_intake`` request parameters so the
          phase adapter is a pure translation.
    """

    repo_path: str
    client_name: Optional[str] = None
    initial_brief: Optional[str] = None
    spec_content: Optional[str] = None
    existing_artifacts: Optional[List[str]] = None


class IntakeOutput(BaseModel):
    """Initial workflow context assembled from the request.

    Postconditions:
        - ``client_context`` is a fully-built ``ClientContext`` (never ``None``).
        - ``initial_brief``/``spec_content`` are normalized to ``""`` when absent,
          matching the legacy context_update shape.
    """

    model_config = {"arbitrary_types_allowed": True}

    client_context: ClientContext
    repo_path: str
    initial_brief: str = ""
    spec_content: str = ""
