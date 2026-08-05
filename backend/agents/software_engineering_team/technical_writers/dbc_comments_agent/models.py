"""Models for the Design by Contract Comments agent."""

from enum import Enum
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from software_engineering_team.shared.models import SystemArchitecture


class DbcCommentsStatus(str, Enum):
    """Progress tracking status for the DbC Comments agent workflow."""

    STARTING = "starting"
    ANALYZING_CODE = "analyzing_code"
    ADDING_COMMENTS = "adding_comments"
    COMMITTING = "committing"
    COMPLETE = "complete"
    FAILED = "failed"


class DbcCommentsInput(BaseModel):
    """Input for the DbC Comments agent."""

    code: str = Field(
        description="The code to review (all files on the branch, concatenated with file headers)",
    )
    language: str = Field(
        default="python",
        description="Primary language: python, typescript, or java",
    )
    task_description: str = Field(
        default="",
        description="The task the coding agent was working on",
    )
    architecture: Optional[SystemArchitecture] = None


class DbcCommentInsertion(BaseModel):
    """A single anchored Design by Contract comment to add to (or replace on) a symbol.

    Replaces the previous whole-file-rewrite contract: instead of asking the
    model to re-emit an entire file, each insertion names exactly one
    file/symbol and carries only the comment/docstring block text to attach
    there -- the surrounding code is never re-emitted. This model is the raw
    shape the LLM is asked to produce; applying it to the original source is
    deterministic, LLM-free logic in :mod:`.merge`.
    """

    file: str = Field(description="Path of the file this insertion applies to")
    symbol: str = Field(
        description="Name of the function/method/class this comment attaches to, or a short "
        "anchor description for a module-level (non-symbol) comment -- accepted aliases "
        "(case-insensitive) are '', 'module docstring', 'module', '<module>', and 'file'; see "
        "merge._MODULE_SYMBOL_ALIASES, which prompts.DBC_COMMENTS_PROMPT's 'module docstring' "
        "example is written to elicit",
    )
    line: Optional[int] = Field(
        default=None,
        description="1-based line number of the symbol's definition in the original file, "
        "when known; None when the model cannot determine it",
    )
    comment: str = Field(
        description="The complete DbC-compliant comment/docstring block text to insert -- "
        "only the comment, never surrounding code",
    )
    action: Literal["add", "update"] = Field(
        default="add",
        description="'add' when the symbol has no existing comment, 'update' when this "
        "replaces an existing comment that was missing DbC sections",
    )


class DbcCommentsOutput(BaseModel):
    """Output from the DbC Comments agent."""

    insertions: List[DbcCommentInsertion] = Field(
        default_factory=list,
        description="Validated, unmerged DbC comment insertions the LLM proposed, regardless "
        "of already_compliant -- the two are not mutually exclusive, since the caller trusts "
        "already_compliant as the model's overall assessment even when it also returned "
        "insertions. Kept for observability even when a given insertion could not be safely "
        "merged into `files` (see `files`' description).",
    )
    files: Dict[str, str] = Field(
        default_factory=dict,
        description="Dict of file_path -> merged file content with the accepted insertions "
        "applied. Deterministically assembled from `insertions` by this agent's own merge "
        "logic -- never re-emitted by the LLM. Only includes a file when at least one of its "
        "insertions was safely anchored and applied and, for '.py' files, the merged result "
        "still parses; a file with no successfully applied insertion is omitted (its pre-DbC "
        "content is simply not returned, never corrupted).",
    )
    rejected_insertions: List[str] = Field(
        default_factory=list,
        description="One human-readable reason per insertion that could not be safely anchored "
        "or merged (unknown file, ambiguous/missing symbol, out-of-range line, duplicate "
        "target, or a merged result that failed the post-merge syntax check). Surfaced rather "
        "than dropped silently, so a rejected insertion is always visible to callers.",
    )
    comments_added: int = Field(
        default=0,
        description="Number of new DbC comments added",
    )
    comments_updated: int = Field(
        default=0,
        description="Number of existing comments updated to comply with DbC",
    )
    already_compliant: bool = Field(
        default=False,
        description="True when all code already has proper DbC comments",
    )
    summary: str = Field(
        default="",
        description="Summary message for the coding agent describing what was changed or praising compliance",
    )
    suggested_commit_message: str = Field(
        default="docs(dbc): add Design by Contract comments",
        description="Conventional Commits format commit message",
    )
