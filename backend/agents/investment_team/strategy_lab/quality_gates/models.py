"""Shared models for quality gate results."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class QualityGateResult(BaseModel):
    """Result of a single quality gate check."""

    gate_name: str
    passed: bool
    details: str
    severity: Literal["info", "warning", "critical"]
    refinement_round: int = 0
