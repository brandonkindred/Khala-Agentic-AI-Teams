"""Models for the Build Fix Specialist agent."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class CodeEdit(BaseModel):
    """A minimal code edit: replace old_text with new_text at the given location."""

    file_path: str = Field(description="Path to the file to edit (e.g. app/main.py)")
    line_start: Optional[int] = Field(
        default=None, description="1-based start line; omit for whole-file replacement"
    )
    line_end: Optional[int] = Field(
        default=None, description="1-based end line; omit for single-line or whole-file"
    )
    old_text: str = Field(description="Exact text to find and replace (must match exactly)")
    new_text: str = Field(description="Replacement text")


class BuildFixInput(BaseModel):
    """Input for the Build Fix Specialist."""

    build_errors: str = Field(description="Build/compiler/test error output")
    failing_test_content: Optional[str] = Field(
        default=None,
        description="Content of the failing test file when error is a test failure",
    )
    affected_files_code: str = Field(
        description="Code for the affected files (e.g. app/main.py, tests/test_foo.py) that need fixing",
    )
    task_description: str = Field(default="", description="Brief task context")


class BuildFixOutput(BaseModel):
    """Output from the Build Fix Specialist."""

    edits: List[CodeEdit] = Field(
        default_factory=list,
        description="List of minimal edits to apply. Each edit specifies file_path, old_text, new_text.",
    )
    summary: str = Field(default="", description="Brief summary of what was fixed")
