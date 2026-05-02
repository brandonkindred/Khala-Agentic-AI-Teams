"""Pydantic contract for the ReleaseNotesAgent.

The ReleaseManagerAgent in ``product_delivery`` adapts heterogeneous
sources (sprint stories, IntegrationOutput.issues, BugReport, raw
DevOps failure dicts) into the lightweight shapes here so the prompt
template stays small and the LLM call is deterministic.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class ReleaseStorySummary(BaseModel):
    """One shipped story, flattened for the prompt."""

    id: str
    title: str
    user_story: str = ""
    acceptance_criteria: List[str] = Field(default_factory=list)


class ReleaseFailure(BaseModel):
    """One Integration / DevOps / QA finding promoted into release notes.

    The shape is intentionally generic — the ReleaseManagerAgent maps
    each upstream model (``IntegrationIssue``, ``BugReport``, raw
    DevOps dicts) onto these fields so the LLM only sees one schema.
    """

    source: str  # "integration" | "qa" | "devops"
    severity: str  # critical | high | medium | low
    summary: str
    location: str = ""
    recommendation: str = ""


class ReleaseNotesInput(BaseModel):
    """All the structured data the LLM sees when drafting release notes.

    ``repo_path`` is informational only — the agent does not write
    files; it returns the markdown body and lets the ReleaseManagerAgent
    decide where to land it (typically ``plan/releases/<version>.md``).
    """

    version: str
    sprint_name: str
    sprint_id: str
    shipped_at_iso: str = ""
    repo_path: str = ""
    stories: List[ReleaseStorySummary] = Field(default_factory=list)
    failures: List[ReleaseFailure] = Field(default_factory=list)


class ReleaseNotesOutput(BaseModel):
    """Markdown body + a one-line summary for log output."""

    markdown: str
    summary: str = ""
    # When ``llm_failed`` is True the markdown was assembled from a
    # deterministic fallback template (no LLM call). The
    # ReleaseManagerAgent still writes the file and records the release
    # — releases must be observable even when the model is down — but
    # callers can surface the degradation in operator logs.
    llm_failed: bool = False
    # Pinned to ``Optional[str]`` so the model survives older Pydantic
    # versions that reject ``str | None`` in nested defaults.
    error: Optional[str] = None
