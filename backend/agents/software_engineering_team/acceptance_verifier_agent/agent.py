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
from typing import Dict, List, Optional

from llm_service import LLMClient
from software_engineering_team.code_review_agent import (
    CodeReviewAgent,
    CodeReviewInput,
    CodeReviewUnavailableError,
    ReviewProfile,
)
from software_engineering_team.code_review_agent.models import CodeReviewIssue

from .models import AcceptanceVerifierInput, AcceptanceVerifierOutput, CriterionStatus

logger = logging.getLogger(__name__)


# The acceptance profile encodes the criterion an issue belongs to as the prefix
# of ``description``, separated from the failure explanation by this delimiter.
# Carrying the criterion in ``description`` (rather than ``category``) keeps
# ``category`` a valid value of the shared output-contract enum AND makes each
# unmet criterion's description distinct, so the coordinator's dedupe (keyed on
# file_path/line/description) never collapses two different unmet criteria into
# one — both failure modes that a category-based tag would have suffered.
_CRITERION_DELIM = " :: "

# AcceptanceVerifierInput.code is a flat blob with no real path; this sentinel
# is the single key under which it's submitted to CodeReviewInput.files (which
# requires a non-empty {path: content} mapping). Mirrors
# CodebaseIndex.EXISTING_CODEBASE_PATH's naming style.
_SUBMISSION_PATH = "<submission>"


def _normalize(text: str) -> str:
    """Whitespace-collapsed, lower-cased form for exact-match comparison.

    Postconditions: returns ``text`` with runs of whitespace collapsed to single
    spaces, trimmed, and lower-cased. Pure; no side effects.
    """
    return " ".join((text or "").split()).lower()


def _attributed_criterion(criteria: List[str], issue: CodeReviewIssue) -> Optional[str]:
    """Return the criterion an ``issue`` is tagged with, or None.

    The acceptance profile prefixes each issue's ``description`` with the verbatim
    criterion followed by ``" :: "``. Rather than split on the first delimiter
    (which breaks when a criterion itself contains ``" :: "``), this compares each
    KNOWN criterion against the description's leading ``" :: "``-delimited
    segments: the criterion is the join of the first ``k`` segments for some
    ``k``. This matches a criterion that contains the delimiter, tolerates an
    empty reason (trailing delimiter), and — because the comparison is exact — a
    criterion that is a prefix of another never steals the longer one's issue.

    Preconditions:
        ``criteria`` are the acceptance criteria; ``issue`` is an engine finding.
    Postconditions:
        Returns the ``criterion`` whose NORMALIZED form (``_normalize(criterion)``,
        not the raw string) is the longest among those equal to the normalized
        join of some leading-segment prefix of the description; None when no
        criterion matches. Blank criteria never match. Pure; no side effects.
    """
    segments = (issue.description or "").split(_CRITERION_DELIM)
    norm_by_criterion = {c: _normalize(c) for c in criteria if _normalize(c)}
    best: Optional[str] = None
    best_len = -1
    for k in range(1, len(segments) + 1):
        candidate = _normalize(_CRITERION_DELIM.join(segments[:k]))
        for criterion, target in norm_by_criterion.items():
            if target == candidate and len(target) > best_len:
                best, best_len = criterion, len(target)
    return best


def _evidence_for(criterion: str, issue: CodeReviewIssue) -> str:
    """Return the failure explanation for an unmet criterion's issue.

    Preconditions:
        ``issue`` is the finding ``_attributed_criterion`` mapped to ``criterion``.
    Postconditions:
        Returns the text after the criterion prefix and its ``" :: "`` delimiter
        (the criterion may itself contain the delimiter, so the matched criterion's
        own delimiter count is skipped). When the description carries no delimiter,
        the whole description is returned (criterion-only tag); when a delimiter is
        present but the remaining text is empty, returns ``"Unmet"`` rather than
        echoing the criterion prefix. Pure; no side effects.
    """
    desc = issue.description or ""
    if _CRITERION_DELIM not in desc:
        return desc.strip() or "Unmet"
    # Count delimiters on the verbatim criterion (not its normalized form): the
    # description is split on the raw delimiter, so the skip must match the raw
    # criterion's delimiter segments — normalization could change the count when
    # the criterion has irregular whitespace around a delimiter.
    skip = criterion.count(_CRITERION_DELIM) + 1
    parts = desc.split(_CRITERION_DELIM)
    tail = _CRITERION_DELIM.join(parts[skip:]).strip() if len(parts) > skip else ""
    return tail or "Unmet"


def derive_per_criterion(
    criteria: List[str], issues: List[CodeReviewIssue]
) -> List[CriterionStatus]:
    """Reconstruct per-criterion status from the engine's flat issue list.

    Preconditions:
        ``criteria`` is the list of acceptance criteria that were verified;
        ``issues`` are the engine findings (one per unmet criterion under the
        ``acceptance`` profile).
    Postconditions:
        Returns one :class:`CriterionStatus` per input criterion in order. Each
        issue is attributed to its longest matching criterion
        (:func:`_attributed_criterion`); a criterion is ``unmet`` iff some issue
        attributes to it (first such issue wins), with ``evidence`` from
        :func:`_evidence_for`, else ``satisfied`` with ``"Satisfied"``. Pure; no
        side effects.
    """
    attribution: Dict[str, CodeReviewIssue] = {}
    for issue in issues:
        criterion = _attributed_criterion(criteria, issue)
        if criterion is not None:
            attribution.setdefault(criterion, issue)
    statuses: List[CriterionStatus] = []
    for criterion in criteria:
        issue = attribution.get(criterion)
        if issue is None:
            statuses.append(
                CriterionStatus(criterion=criterion, satisfied=True, evidence="Satisfied")
            )
        else:
            statuses.append(
                CriterionStatus(
                    criterion=criterion,
                    satisfied=False,
                    evidence=_evidence_for(criterion, issue),
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

    def __init__(self, llm_client: LLMClient) -> None:
        """Store the LLM client the engine will run on.

        Preconditions:
            ``llm_client`` is not None — the gate fails fast rather than deferring
            a confusing error to the first ``run`` call (matches ``ChangeReviewAgent``).
            Enforced with an explicit ``raise`` (not ``assert``) so the boundary
            check survives ``python -O``.
        """
        if llm_client is None:
            raise ValueError("llm_client is required")
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
                    files={_SUBMISSION_PATH: input_data.code},
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

        # Conservative guard: the acceptance profile emits one issue per UNMET
        # criterion, tagged with the verbatim criterion. An issue that attributes
        # to no criterion means the reviewer flagged something unmet that we
        # cannot map to a specific criterion (e.g. the model dropped the criterion
        # prefix). Rather than let that unmet finding pass silently, block.
        unattributed = [
            i
            for i in result.issues
            if _attributed_criterion(input_data.acceptance_criteria, i) is None
        ]
        summary = result.summary or "Acceptance verification complete"
        if unattributed:
            all_satisfied = False
            summary = (
                f"{summary} ({len(unattributed)} finding(s) could not be attributed to a "
                "specific criterion; treated as unmet)."
            )

        logger.info(
            "AcceptanceVerifier: %s/%s satisfied, %s unattributed, all_satisfied=%s",
            sum(1 for c in per_criterion if c.satisfied),
            len(per_criterion),
            len(unattributed),
            all_satisfied,
        )
        return AcceptanceVerifierOutput(
            all_satisfied=all_satisfied,
            per_criterion=per_criterion,
            summary=summary,
        )
