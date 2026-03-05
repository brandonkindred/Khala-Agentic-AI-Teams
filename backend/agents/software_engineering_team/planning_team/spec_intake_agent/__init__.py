"""Spec Intake and Validation agent: validates spec, produces REQ-IDs, glossary, assumptions."""

from .agent import SpecIntakeAgent
from .models import (
    SpecIntakeInput,
    SpecIntakeOutput,
    build_compact_spec_for_planning,
    validated_spec_to_requirements,
)

__all__ = [
    "SpecIntakeAgent",
    "SpecIntakeInput",
    "SpecIntakeOutput",
    "build_compact_spec_for_planning",
    "validated_spec_to_requirements",
]
