"""
Structured exceptions for the blogging pipeline.

Provides a hierarchy of exceptions that ensure errors are never swallowed silently.
All exceptions include context for debugging and job status updates.
"""

from __future__ import annotations

from typing import Optional


class BloggingError(Exception):
    """Base exception for all blogging pipeline errors.

    All blogging errors should inherit from this class to enable
    consistent error handling at the orchestrator level.
    """

    def __init__(
        self,
        message: str,
        *,
        phase: Optional[str] = None,
        cause: Optional[Exception] = None,
    ):
        super().__init__(message)
        self.message = message
        self.phase = phase
        self.cause = cause

    def __str__(self) -> str:
        if self.phase:
            return f"[{self.phase}] {self.message}"
        return self.message


class ResearchError(BloggingError):
    """Research phase failed.

    Raised when web search, arXiv search, or document fetching fails
    in a way that prevents the pipeline from continuing.
    """

    def __init__(
        self,
        message: str,
        *,
        sources_found: int = 0,
        cause: Optional[Exception] = None,
    ):
        super().__init__(message, phase="research", cause=cause)
        self.sources_found = sources_found


class PlanningError(BloggingError):
    """Planning phase failed (content plan could not be produced or refined)."""

    def __init__(
        self,
        message: str,
        *,
        cause: Optional[Exception] = None,
        failure_reason: Optional[str] = None,
    ):
        super().__init__(message, phase="planning", cause=cause)
        self.failure_reason = failure_reason


class DraftError(BloggingError):
    """Draft generation or revision failed."""

    def __init__(
        self,
        message: str,
        *,
        iteration: int = 0,
        cause: Optional[Exception] = None,
        phase: str = "draft",
    ):
        super().__init__(message, phase=phase, cause=cause)
        self.iteration = iteration


class CopyEditError(BloggingError):
    """Copy editing phase failed."""

    def __init__(
        self,
        message: str,
        *,
        iteration: int = 0,
        cause: Optional[Exception] = None,
    ):
        super().__init__(message, phase="copy_edit", cause=cause)
        self.iteration = iteration


class ComplianceError(BloggingError):
    """Compliance check failed with unrecoverable violations.

    Raised when the compliance agent cannot evaluate the draft
    or when violations cannot be automatically resolved.
    """

    def __init__(
        self,
        message: str,
        *,
        violation_count: int = 0,
        cause: Optional[Exception] = None,
    ):
        super().__init__(message, phase="compliance", cause=cause)
        self.violation_count = violation_count


class FactCheckError(BloggingError):
    """Fact-check identified high-risk claims or failed to run.

    Raised when fact checking cannot complete or finds
    claims that cannot be verified against allowed sources.
    """

    def __init__(
        self,
        message: str,
        *,
        unverified_claims: int = 0,
        high_risk_count: int = 0,
        cause: Optional[Exception] = None,
    ):
        super().__init__(message, phase="fact_check", cause=cause)
        self.unverified_claims = unverified_claims
        self.high_risk_count = high_risk_count


class ValidationError(BloggingError):
    """Content validation failed (word count, structure, etc.)."""

    def __init__(
        self,
        message: str,
        *,
        failed_checks: Optional[list] = None,
        cause: Optional[Exception] = None,
    ):
        super().__init__(message, phase="validation", cause=cause)
        self.failed_checks = failed_checks or []


class PublicationError(BloggingError):
    """Publication preparation failed."""

    def __init__(self, message: str, *, cause: Optional[Exception] = None):
        super().__init__(message, phase="publication", cause=cause)
