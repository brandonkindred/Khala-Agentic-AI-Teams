"""SE team API — architect design route: synchronous architecture generation from a spec."""

import logging

from fastapi import APIRouter, HTTPException

from software_engineering_team.api.models import (
    ArchitectDesignRequest,
    ArchitectDesignResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/architect/design",
    response_model=ArchitectDesignResponse,
    summary="Generate architecture from spec",
    description="Parse spec, run Architecture Expert agent, return architecture documents and diagrams. "
    "Uses heuristic spec parsing by default; set use_llm=true for LLM-based parsing.",
)
def architect_design(request: ArchitectDesignRequest) -> ArchitectDesignResponse:
    """Generate software architecture from a product specification."""
    try:
        from architecture_expert import ArchitectureExpertAgent
        from architecture_expert.models import ArchitectureInput
        from spec_parser import parse_spec_with_llm

        from llm_service import get_client
    except (
        ImportError
    ) as e:  # pragma: no cover  # defensive: architect deps always importable in-env
        logger.exception("Failed to import architect dependencies")
        raise HTTPException(status_code=500, detail=f"Architect agent unavailable: {e}") from e

    if not request.spec or not request.spec.strip():
        raise HTTPException(status_code=400, detail="Spec text is required")

    try:  # pragma: no cover  # integration-only: spec parse + architecture both call live LLM
        llm = get_client("architecture")
        requirements = parse_spec_with_llm(request.spec.strip(), llm)

        arch_agent = ArchitectureExpertAgent(get_client("architecture"))
        arch_input = ArchitectureInput(requirements=requirements)
        arch_output = arch_agent.run(arch_input)
        architecture = arch_output.architecture

        components = [
            c.model_dump() if hasattr(c, "model_dump") else c.dict()
            for c in architecture.components
        ]

        return ArchitectDesignResponse(
            overview=architecture.overview,
            architecture_document=architecture.architecture_document or "",
            components=components,
            diagrams=architecture.diagrams or {},
            decisions=architecture.decisions or [],
            tenancy_model=getattr(architecture, "tenancy_model", "") or "",
            reliability_model=getattr(architecture, "reliability_model", "") or "",
            summary=arch_output.summary or "",
        )
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Architect design failed")
        raise HTTPException(status_code=500, detail=str(e)) from e
