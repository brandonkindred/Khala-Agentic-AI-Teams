"""
Minimal shared contract for quality-gate review results.

For future consistency across Code Review, QA, Security, Accessibility, Acceptance Verifier.
Existing agents use different attribute names for issues (issues, bugs_found, vulnerabilities)
but all have approved: bool. New agents should prefer the approved + issues pattern.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ReviewResult(Protocol):
    """
    Minimal contract for a quality-gate review output.

    All quality-gate agents produce outputs with:
    - approved: bool — True when code passes (no blocking issues)
    - An issues-like list (attribute name varies: issues, bugs_found, vulnerabilities)
    """
    approved: bool
