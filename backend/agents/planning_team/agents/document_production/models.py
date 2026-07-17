"""Boundary contracts for the Document Production agent (§1 typed Input/Output).

Reuses the shared ``HandoffPackage`` domain model from ``planning_team.models``.
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, Field

from planning_team.models import HandoffPackage


class DocumentProductionInput(BaseModel):
    """Inputs for producing the planning documents and handoff.

    ``client_context`` carries the raw upstream value (``ClientContext``, dict, or
    ``None``); the agent normalizes it via the shared ``as_client_context`` and also
    inspects the raw value for the architecture-step dict branch.
    """

    model_config = {"arbitrary_types_allowed": True}

    repo_path: str = ""
    client_context: Any = None
    spec_content: str = ""
    initial_brief: str = ""
    use_product_analysis: bool = True


class DocumentProductionOutput(BaseModel):
    """The handoff package plus the on-disk artifact index.

    ``artifacts`` is an inherently variable path index (its keys depend on which
    documents were written and whether PRA ran), so it is carried as a documented
    ``Dict[str, Any]`` — mirroring the legacy ``artifacts`` mapping byte-for-byte.
    ``handoff_package`` becomes ``context_update["handoff_package"]``.
    """

    model_config = {"arbitrary_types_allowed": True}

    handoff_package: HandoffPackage
    artifacts: Dict[str, Any] = Field(default_factory=dict)
