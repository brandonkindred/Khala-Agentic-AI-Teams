"""Change review agent.

A thin adapter over the shared code-review engine
(``code_review_agent.CodeReviewAgent``): it routes the DevOps change-review gate
through the engine with the ``devops_maintainability`` profile instead of running
its own one-shot review. The engine supplies chunking, false-positive filtering,
and synthesis; this adapter only translates the artifacts into a review input and
the engine's flat issue list back into ``ReviewFinding``s, preserving the gate's
public class, ``run`` signature, output model, and blocking semantics.
"""

from __future__ import annotations

import logging
from typing import get_args

from llm_service import LLMClient
from software_engineering_team.code_review_agent import (
    CodeReviewAgent,
    CodeReviewInput,
    CodeReviewUnavailableError,
    ReviewProfile,
)
from software_engineering_team.code_review_agent.models import (
    CodeReviewIssue,
    CodeReviewIssueSeverity,
)
from software_engineering_team.devops_team.models import ReviewFinding
from software_engineering_team.shared.security_service import derive_approved, is_blocking

from .models import ChangeReviewInput, ChangeReviewOutput

logger = logging.getLogger(__name__)

_ENGINE_SEVERITIES = frozenset(get_args(CodeReviewIssueSeverity))
_REVIEW_FINDING_SEVERITIES = frozenset(get_args(ReviewFinding.model_fields["severity"].annotation))

# Engine severities with no identically-named ReviewFinding counterpart; every other
# engine severity maps to itself. ``info`` maps to ``low`` (its nearest non-blocking
# neighbour). If code_review_agent ever adds a severity that's neither already valid
# in ReviewFinding nor covered here, the assertion below fails at import time instead
# of this table silently drifting out of sync in production.
_EXPLICIT_SEVERITY_REMAP = {"info": "low"}

_unmapped_engine_severities = (
    _ENGINE_SEVERITIES - _REVIEW_FINDING_SEVERITIES - set(_EXPLICIT_SEVERITY_REMAP)
)
assert not _unmapped_engine_severities, (
    f"code_review_agent severities {sorted(_unmapped_engine_severities)} have no ReviewFinding "
    "counterpart and no entry in change_review_agent._EXPLICIT_SEVERITY_REMAP"
)

_SEVERITY_MAP = {s: _EXPLICIT_SEVERITY_REMAP.get(s, s) for s in _ENGINE_SEVERITIES}


def _normalize_severity(severity: str) -> str:
    """Map an engine severity onto a ``ReviewFinding`` severity.

    Preconditions:
        ``severity`` is a string (any case/spacing); engine values are
        critical|high|medium|low|info.
    Postconditions:
        Returns a member of ``ReviewFinding``'s severity literal
        (critical|high|medium|low|minor|nit). ``info`` maps to ``low``; an
        already-valid value passes through; anything unrecognized logs a warning
        and falls back to ``low`` so a stray value never fails ``ReviewFinding``
        validation (the warning surfaces a possibly-new engine severity).
    """
    sev = (severity or "").strip().lower()
    sev = _SEVERITY_MAP.get(sev, sev)
    if sev not in _REVIEW_FINDING_SEVERITIES:
        # A severity the engine emits but ReviewFinding doesn't know — likely a new
        # engine level. Warn so it isn't silently treated as non-blocking, then fall
        # back to the safe, validation-passing default.
        logger.warning("ChangeReview: unrecognized severity %r mapped to 'low'", severity)
        return "low"
    return sev


def _to_finding(index: int, issue: CodeReviewIssue) -> ReviewFinding:
    """Translate one engine ``CodeReviewIssue`` into a ``ReviewFinding``.

    Preconditions:
        ``index`` is a non-negative int unique within one review (used to mint a
        stable ``finding_id``); ``issue`` is an engine finding.
    Postconditions:
        Returns a ``ReviewFinding`` whose ``blocking`` flag is the canonical
        ``is_blocking(issue.severity)`` (critical/high block), preserving the
        gate's "approve unless a blocking finding exists" rule. Pure.
    """
    return ReviewFinding(
        finding_id=f"cr-{index}",
        severity=_normalize_severity(issue.severity),
        area=issue.category or "",
        file_ref=issue.file_path or "",
        issue=issue.description or "",
        recommended_fix=issue.suggestion or "",
        blocking=is_blocking(issue.severity),
    )


class ChangeReviewAgent:
    """Routes the DevOps change-review gate through the shared review engine.

    Invariants:
        - ``run`` returns a ``ChangeReviewOutput`` whose ``approved`` is False
          whenever any finding blocks (critical/high), matching the legacy
          "approve unless a blocking finding exists" contract.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        # Explicit raise (not assert) so the precondition survives ``python -O``.
        if llm_client is None:
            raise ValueError("llm_client is required")
        self.llm = llm_client

    def run(self, input_data: ChangeReviewInput) -> ChangeReviewOutput:
        """Review the change's artifacts and return approval plus findings.

        Preconditions:
            ``input_data`` is a ``ChangeReviewInput``; ``artifacts`` maps file
            paths to their content (possibly empty).
        Postconditions:
            - With no artifacts, returns ``approved=True`` and no findings (there
              is nothing to block on), preserving the legacy empty-input result.
            - Otherwise returns the engine's findings mapped to ``ReviewFinding``
              and ``approved = derive_approved(findings, llm_approved=engine_approved)``,
              so approval is False iff a blocking finding exists or the engine
              rejected. A ``CodeReviewUnavailableError`` (the review could not be
              run) degrades to ``approved=True`` with an explanatory summary
              rather than crashing the DevOps pipeline; any other exception is a
              defect and propagates unchanged.
        """
        if not input_data.artifacts:
            return ChangeReviewOutput(approved=True, findings=[], summary="No artifacts to review.")

        try:
            result = CodeReviewAgent(self.llm).run(
                CodeReviewInput(
                    files=dict(input_data.artifacts),
                    task_description=input_data.task_description,
                    profile=ReviewProfile.DEVOPS_MAINTAINABILITY,
                )
            )
        except CodeReviewUnavailableError as exc:
            return ChangeReviewOutput(
                approved=True,
                findings=[],
                summary=f"Change review unavailable: {exc}",
            )

        findings = [_to_finding(i, issue) for i, issue in enumerate(result.issues)]
        return ChangeReviewOutput(
            approved=derive_approved(findings, llm_approved=result.approved),
            findings=findings,
            summary=result.summary,
        )
