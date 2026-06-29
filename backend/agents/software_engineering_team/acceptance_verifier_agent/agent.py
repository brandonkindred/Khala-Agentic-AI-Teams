"""Acceptance Criteria Verifier agent.

A thin adapter over the shared code-review engine
(``code_review_agent.CodeReviewAgent``): it routes acceptance verification
through the engine with the ``acceptance`` profile, which instructs the reviewer
to emit exactly one issue per *unmet* acceptance criterion (tagging each issue's
``category`` with the verbatim criterion). The adapter then derives the
per-criterion status from those issues. The engine supplies chunking,
false-positive filtering (which runs here, so a criterion whose evidence exists
elsewhere in the codebase is correctly treated as satisfied), and synthesis,
while this module keeps the gate's public class, ``run`` signature, output model,
empty-criteria short-circuit, and failure fallback.
"""

from __future__ import annotations

import logging
from typing import List

from code_review_agent import (
    CodeReviewAgent,
    CodeReviewInput,
    CodeReviewUnavailableError,
    ReviewProfile,
)
from code_review_agent.models import CodeReviewIssue

from .models import AcceptanceVerifierInput, AcceptanceVerifierOutput, CriterionStatus

logger = logging.getLogger(__name__)


def _normalize(text: str) -> str:
    """Whitespace-collapsed, lower-cased form for exact-match comparison.

    Postconditions: returns ``text`` with runs of whitespace collapsed to single
    spaces, trimmed, and lower-cased. Pure; no side effects.
    """
    return " ".join((text or "").split()).lower()


def _matches_criterion(criterion: str, issue: CodeReviewIssue) -> bool:
    """Return whether ``issue`` reports ``criterion`` as unmet.

    Matching is a normalized *exact* comparison of the issue's ``category``
    against the criterion. The ``acceptance`` profile instructs the model to set
    ``category`` to the verbatim criterion text, so exact match is the reliable
    signal. Substring matching is deliberately NOT used: it mis-fires when one
    criterion is a substring of another (an issue tagged with the longer
    criterion would also match the shorter one), which would mark a satisfied
    criterion as unmet and falsely reject a valid change.

    Preconditions:
        ``criterion`` is an acceptance-criterion string; ``issue`` is an engine
        finding produced under the ``acceptance`` profile.
    Postconditions:
        Returns True iff the normalized ``issue.category`` equals the normalized
        ``criterion`` (a blank criterion never matches). Pure; no side effects.
    """
    target = _normalize(criterion)
    if not target:
        return False
    return _normalize(issue.category) == target


def derive_per_criterion(
    criteria: List[str], issues: List[CodeReviewIssue]
) -> List[CriterionStatus]:
    """Reconstruct per-criterion status from the engine's flat issue list.

    Preconditions:
        ``criteria`` is the list of acceptance criteria that were verified;
        ``issues`` are the engine findings (one per unmet criterion under the
        ``acceptance`` profile).
    Postconditions:
        Returns one :class:`CriterionStatus` per input criterion in order. A
        criterion is ``satisfied`` iff no issue matches it (see
        :func:`_matches_criterion`); when unmet, its ``evidence`` is the matching
        issue's description, otherwise ``"Satisfied"``. Pure; no side effects.
    """
    statuses: List[CriterionStatus] = []
    for criterion in criteria:
        match = next((i for i in issues if _matches_criterion(criterion, i)), None)
        if match is None:
            statuses.append(
                CriterionStatus(criterion=criterion, satisfied=True, evidence="Satisfied")
            )
        else:
            statuses.append(
                CriterionStatus(
                    criterion=criterion,
                    satisfied=False,
                    evidence=match.description or "Unmet",
                )
            )
    return statuses


class AcceptanceVerifierAgent:
    """
    Verifies that delivered code satisfies each acceptance criterion.
    Returns per-criterion status with evidence.

    Invariants:
        - ``run`` returns ``all_satisfied`` iff every criterion's derived status
          is satisfied, preserving the gate's "block unless all criteria are
          met" contract.
    """

    def __init__(self, llm_client=None) -> None:
        """Store the LLM client the engine will run on.

        Preconditions:
            ``llm_client`` is not None — the gate fails fast rather than deferring
            a confusing error to the first ``run`` call (matches ``ChangeReviewAgent``).
        """
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: AcceptanceVerifierInput) -> AcceptanceVerifierOutput:
        """Verify each acceptance criterion against the code.

        Preconditions:
            ``input_data`` is an ``AcceptanceVerifierInput``; ``acceptance_criteria``
            may be empty.
        Postconditions:
            - With no criteria, returns ``all_satisfied=True`` and an empty list
              without invoking the engine (no LLM round-trip).
            - With criteria but no code, returns every criterion unsatisfied with
              ``"No code provided"`` evidence and ``all_satisfied=False``, again
              without invoking the engine.
            - Otherwise returns one ``CriterionStatus`` per criterion derived from
              the engine's findings, with ``all_satisfied`` true iff all are
              satisfied. A ``CodeReviewUnavailableError`` from the engine (the
              review could not be run) returns ``all_satisfied=False`` with an
              explanatory summary; any other exception is a defect and
              propagates unchanged rather than being masked as "unsatisfied".
        """
        # Short-circuit on empty criteria — avoids an unnecessary engine round-trip.
        if not input_data.acceptance_criteria:
            return AcceptanceVerifierOutput(
                all_satisfied=True, per_criterion=[], summary="No criteria to verify"
            )

        # Short-circuit on missing code — there is nothing to verify the criteria
        # against, so every criterion is unmet. Reporting this explicitly is
        # clearer (and cheaper) than letting the engine reject an empty submission.
        if not (input_data.code or "").strip():
            return AcceptanceVerifierOutput(
                all_satisfied=False,
                per_criterion=[
                    CriterionStatus(criterion=c, satisfied=False, evidence="No code provided")
                    for c in input_data.acceptance_criteria
                ],
                summary="No code provided to verify against acceptance criteria",
            )

        logger.info(
            "AcceptanceVerifier: checking %s criteria against %s chars of code",
            len(input_data.acceptance_criteria),
            len(input_data.code or ""),
        )

        try:
            result = CodeReviewAgent(self.llm).run(
                CodeReviewInput(
                    code=input_data.code or "",
                    task_description=input_data.task_description,
                    acceptance_criteria=input_data.acceptance_criteria,
                    spec_content=input_data.spec_content,
                    architecture=input_data.architecture,
                    language=input_data.language,
                    profile=ReviewProfile.ACCEPTANCE,
                )
            )
        except CodeReviewUnavailableError as exc:
            logger.warning(
                "AcceptanceVerifier: review engine unavailable (%s); returning fallback", exc
            )
            return AcceptanceVerifierOutput(
                all_satisfied=False,
                per_criterion=[],
                summary=f"Acceptance verification failed: {exc}",
            )

        per_criterion = derive_per_criterion(input_data.acceptance_criteria, result.issues)
        all_satisfied = all(c.satisfied for c in per_criterion)

        logger.info(
            "AcceptanceVerifier: %s/%s satisfied, all_satisfied=%s",
            sum(1 for c in per_criterion if c.satisfied),
            len(per_criterion),
            all_satisfied,
        )
        return AcceptanceVerifierOutput(
            all_satisfied=all_satisfied,
            per_criterion=per_criterion,
            summary=result.summary or "Acceptance verification complete",
        )
