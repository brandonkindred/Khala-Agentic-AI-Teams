"""
Quality gates: cross-cutting review agents (Code Review, QA, Security, Acceptance Verifier, DbC).

These agents are invoked inside backend and frontend per-task workflows. None are task assignees.
Accessibility Expert lives under frontend_team/ but is conceptually part of this set for frontend.

Shared contract: see quality_gates.protocols.ReviewResult (approved: bool, issues-like list).
"""

from __future__ import annotations

# Re-exports for discoverability. Orchestrator and other callers may use these or import directly.
from software_engineering_team.acceptance_verifier_agent import AcceptanceVerifierAgent
from software_engineering_team.code_review_agent import CodeReviewAgent, CodeReviewInput
from software_engineering_team.qa_agent import QAExpertAgent, QAInput
from software_engineering_team.security_agent import CybersecurityExpertAgent, SecurityInput
from software_engineering_team.technical_writers.dbc_comments_agent import (
    DbcCommentsAgent,
    DbcCommentsInput,
)

from . import protocols

__all__ = [
    "CodeReviewAgent",
    "CodeReviewInput",
    "QAExpertAgent",
    "QAInput",
    "CybersecurityExpertAgent",
    "SecurityInput",
    "AcceptanceVerifierAgent",
    "DbcCommentsAgent",
    "DbcCommentsInput",
    "protocols",
]
