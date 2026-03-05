"""
Validator report models for deterministic checks.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

Status = str  # "PASS" or "FAIL"


class CheckResult(BaseModel):
    """Result of a single validator check."""

    name: str = Field(..., description="Check identifier (e.g. banned_phrases, reading_level).")
    status: str = Field(..., description="PASS or FAIL.")
    details: Optional[Dict[str, Any]] = Field(
        None,
        description="Check-specific details (e.g. matches, fk_grade, violations).",
    )


class ValidatorReport(BaseModel):
    """Aggregate report from all deterministic validators."""

    status: str = Field(..., description="Overall PASS if all checks pass, else FAIL.")
    checks: List[CheckResult] = Field(
        default_factory=list,
        description="Results for each check.",
    )
