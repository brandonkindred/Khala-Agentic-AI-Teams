"""Models for the Task Generator agent."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from software_engineering_team.shared.models import ProductRequirements, SystemArchitecture

# Caps for context (chars)
MAX_SPEC_TRUNCATED_CHARS = 20_000
MAX_EXISTING_CODE_CHARS = 20_000
MAX_FEATURES_DOC_CHARS = 15_000
MAX_ARCH_DOC_CHARS = 5_000


class TaskGeneratorInput(BaseModel):
    """Input for the Task Generator agent. All fields are capped."""

    requirements: ProductRequirements
    merged_spec_analysis: str = Field(
        ...,
        description="Merged spec analysis from SpecAnalysisMerger (JSON string)",
    )
    codebase_analysis: str = Field(
        default="",
        description="Codebase analysis from Tech Lead Step 1",
    )
    spec_content_truncated: str = Field(
        default="",
        description="Truncated spec (first 20K chars)",
    )
    existing_codebase: str = Field(
        default="",
        description="Truncated existing code (20K chars)",
    )
    project_overview: Optional[Dict[str, Any]] = None
    features_doc: str = Field(
        default="",
        description="Features and functionality doc (capped 15K)",
    )
    architecture: Optional[SystemArchitecture] = None
    repo_path: str = Field(default="")
    open_questions: Optional[List[str]] = Field(
        None,
        description="Open questions from Spec Intake; resolve with enterprise-informed defaults and emit tasks",
    )
    assumptions: Optional[List[str]] = Field(
        None,
        description="Assumptions from Spec Intake; may extend when resolving open questions",
    )
    resolved_questions: Optional[List[Dict[str, str]]] = Field(
        None,
        description="User-provided answers from clarification chat; when set, use these instead of resolving with defaults",
    )
