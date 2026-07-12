"""Models for the Build Fix Specialist agent."""

from __future__ import annotations

from typing import Any, List, Optional

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


def parse_code_edits(data: Any) -> List[CodeEdit]:
    """Build the well-formed :class:`CodeEdit` objects from a parsed model reply.

    Shared by the Build Fix Specialist and the lint-fix tool agent, which both
    turn a ``{"edits": [...]}`` reply into ``CodeEdit`` objects with the same
    well-formedness guard.

    Preconditions: ``data`` is the parsed JSON reply (a dict is expected).
    Postconditions: returns one :class:`CodeEdit` per ``data["edits"]`` entry that
    is a dict carrying a truthy ``file_path`` and both ``old_text`` and
    ``new_text``; malformed entries (and a non-dict ``data``) are skipped. Never
    raises.
    """
    edits: List[CodeEdit] = []
    if not isinstance(data, dict):
        return edits
    for e in data.get("edits") or []:
        if isinstance(e, dict) and e.get("file_path") and "old_text" in e and "new_text" in e:
            edits.append(
                CodeEdit(
                    file_path=e["file_path"],
                    line_start=e.get("line_start"),
                    line_end=e.get("line_end"),
                    old_text=e["old_text"],
                    new_text=e["new_text"],
                )
            )
    return edits


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
