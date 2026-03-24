"""Shared TaskPlan model for per-task implementation planning.

Used by backend and frontend coding agents to structure the plan produced
before code generation. The plan drives implementation: output must realize
what_changes and tests_needed, and use the stated algorithms/data structures.
"""

from __future__ import annotations

from typing import Any, Dict, List, Union

from pydantic import BaseModel, Field


class TaskPlan(BaseModel):
    """Structured plan for a single task implementation.

    Produced by the planning step before code generation. The coding agent
    must implement the task according to this plan.
    """

    feature_intent: str = Field(
        default="",
        description="What the feature is meant to achieve from the task details.",
    )
    what_changes: Union[str, List[str]] = Field(
        default="",
        description="Files/modules to add or modify; high-level change list.",
    )
    algorithms_data_structures: str = Field(
        default="",
        description="Key algorithmic or data-structure choices for efficiency/correctness.",
    )
    tests_needed: str = Field(
        default="",
        description="What unit/integration tests to add or update.",
    )

    def to_markdown(self) -> str:
        """Serialize the plan to markdown for injection into the code-generation prompt."""
        parts: List[str] = []
        if self.feature_intent:
            parts.append(f"**Feature intent:** {self.feature_intent}")
        if self.what_changes:
            changes = self.what_changes
            if isinstance(changes, list):
                changes = "\n".join(f"- {c}" for c in changes if c)
            parts.append(f"**What changes:**\n{changes}")
        if self.algorithms_data_structures:
            parts.append(f"**Algorithms/data structures:** {self.algorithms_data_structures}")
        if self.tests_needed:
            parts.append(f"**Tests needed:** {self.tests_needed}")
        return "\n\n".join(parts) if parts else ""

    @classmethod
    def from_llm_json(cls, data: Dict[str, Any]) -> "TaskPlan":
        """Parse LLM JSON output into TaskPlan. Tolerates missing or malformed keys."""
        what = data.get("what_changes", "")
        if isinstance(what, list):
            pass
        elif isinstance(what, str):
            pass
        else:
            what = str(what) if what else ""
        return cls(
            feature_intent=str(data.get("feature_intent", "") or ""),
            what_changes=what,
            algorithms_data_structures=str(data.get("algorithms_data_structures", "") or ""),
            tests_needed=str(data.get("tests_needed", "") or ""),
        )
