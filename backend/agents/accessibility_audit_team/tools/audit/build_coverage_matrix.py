"""
Tool: audit.build_coverage_matrix

Produce SC x surface x journey coverage matrix.
"""

from typing import List, Optional
import uuid

from pydantic import BaseModel, Field

from ...models import (
    CoverageMatrix,
    CoverageRow,
    Surface,
    VerificationDepth,
)
from ...wcag_criteria import get_level_a_aa_criteria, WCAG_22_CRITERIA


class BuildCoverageMatrixInput(BaseModel):
    """Input for building a coverage matrix."""

    audit_id: str = Field(..., description="Audit identifier")
    surfaces: List[str] = Field(
        default_factory=lambda: ["web"],
        description="Surfaces to test: web, ios, android, pdf",
    )
    journeys: List[str] = Field(
        default_factory=list, description="User journeys to cover"
    )
    wcag_version: str = Field(default="2.2", description="WCAG version")
    focus_sc: List[str] = Field(
        default_factory=list,
        description="Specific SCs to focus on (empty = all Level A/AA)",
    )


class BuildCoverageMatrixOutput(BaseModel):
    """Output from building a coverage matrix."""

    coverage_matrix: CoverageMatrix
    matrix_ref: str = Field(default="", description="Reference to the matrix")
    total_cells: int = Field(default=0, description="Total matrix cells")
    sc_count: int = Field(default=0, description="Number of success criteria")


async def build_coverage_matrix(
    input_data: BuildCoverageMatrixInput,
) -> BuildCoverageMatrixOutput:
    """
    Build a coverage matrix mapping success criteria to surfaces and journeys.

    This tool creates a structured matrix showing which WCAG success criteria
    need to be tested against which surfaces and user journeys.
    """
    # Determine which SCs to include
    if input_data.focus_sc:
        sc_list = [
            WCAG_22_CRITERIA[sc]
            for sc in input_data.focus_sc
            if sc in WCAG_22_CRITERIA
        ]
    else:
        sc_list = get_level_a_aa_criteria()

    # Parse surfaces
    surfaces = [Surface(s) for s in input_data.surfaces if s in Surface.__members__.values()]
    if not surfaces:
        surfaces = [Surface.WEB]

    # Build matrix rows
    rows = []
    for criterion in sc_list:
        row = CoverageRow(
            sc=criterion.sc,
            sc_name=criterion.name,
            surfaces=surfaces,
            journeys=input_data.journeys,
            depth=VerificationDepth.SIGNAL,
            status="not_started",
            findings_count=0,
        )
        rows.append(row)

    # Create the matrix
    matrix_ref = f"coverage_matrix_{input_data.audit_id}_{uuid.uuid4().hex[:8]}"
    coverage_matrix = CoverageMatrix(
        matrix_ref=matrix_ref,
        audit_id=input_data.audit_id,
        wcag_version=input_data.wcag_version,
        rows=rows,
    )

    return BuildCoverageMatrixOutput(
        coverage_matrix=coverage_matrix,
        matrix_ref=matrix_ref,
        total_cells=len(rows) * len(surfaces) * max(len(input_data.journeys), 1),
        sc_count=len(rows),
    )
