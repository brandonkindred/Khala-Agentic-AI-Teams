"""Structured-output payloads for the refactored Nutritionist agent.

SPEC-004 §4.6. The LLM narrator produces two different payload shapes
depending on cohort:

- ``NarrativePayload``: authored alongside numeric targets from the
  calculator. Prose around numbers — never numbers.
- ``GuidanceOnlyPayload``: authored when the calculator refuses to
  emit targets (minors, CKD 4-5, incomplete profiles). Qualitative
  food-group guidance with an explicit clinician-consult note.

Both are strict Pydantic schemas so the llm_service.complete_validated
contract (PR #184) rejects malformed output and triggers one
corrective retry before falling through to an empty-narrative fallback.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class NarrativePayload(BaseModel):
    """LLM output when the calculator produced numeric targets."""

    balance_guidelines: List[str] = Field(default_factory=list)
    foods_to_emphasize: List[str] = Field(default_factory=list)
    foods_to_avoid: List[str] = Field(default_factory=list)
    notes: str = ""
    summary: str = ""  # 1-2 sentences, user-facing

    model_config = {"extra": "forbid"}


class GuidanceOnlyPayload(BaseModel):
    """LLM output when numeric targets are not being emitted.

    ``clinician_note`` is **required** non-empty on this path — the
    calculator already told us the user should work with a clinician,
    and the narrator must surface that to them explicitly.
    """

    balance_guidelines: List[str] = Field(default_factory=list)
    foods_to_emphasize: List[str] = Field(default_factory=list)
    foods_to_avoid: List[str] = Field(default_factory=list)
    notes: str = ""
    clinician_note: str = Field(..., min_length=1)

    model_config = {"extra": "forbid"}
