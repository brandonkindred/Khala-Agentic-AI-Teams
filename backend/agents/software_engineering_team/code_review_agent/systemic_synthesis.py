"""Best-effort LLM synthesis of systemic / cross-cutting PR review findings.

A code review over a pull request can surface several independently-true
findings that, taken together, point at one shared root cause — the same kind
of mistake repeated across call sites, or conceptually similar issues in
different functions/files that share one broken invariant. Individually those
findings are each posted as their own PR comment; nothing calls out the
pattern connecting them.

This module is the single best-effort LLM pass that draws that connection.
:func:`synthesize_systemic_findings` takes the PR's already in-scope,
already-deduped findings, pre-clusters them with
``github_source.issue_proposals.group_similar_findings`` (as a hint, not a
hard filter — the whole point is also catching conceptually-similar findings
that land in *different* clusters), and makes one ``complete_json`` call
asking the model to name genuinely cross-cutting patterns.

The pass is **fail-safe by design**: a missing client, too few findings, or
any failure (LLM error, malformed reply) degrades to an empty list. Never
raises.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from llm_service.interface import LLMClient
from software_engineering_team.github_source import group_similar_findings, scrub_token_from_text
from software_engineering_team.shared.single_shot_review import run_single_shot_review

from ._llm_client_utils import is_unscripted_dummy
from ._prompt_utils import _render_finding_block
from .models import CodeReviewInput, CodeReviewIssue
from .prompts import (
    SYSTEMIC_SYNTHESIS_FORMATTING_INSTRUCTIONS,
    SYSTEMIC_SYNTHESIS_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)

# Minimum number of in-scope findings before synthesis is even attempted — a
# systemic pattern is, by definition, evidenced by at least two findings.
MIN_FINDINGS_FOR_SYNTHESIS = 2

# Hard cap on how many findings are inlined into the single synthesis prompt,
# so an unusually large review cannot blow the prompt context. Findings beyond
# the cap are simply not offered to the synthesis pass (best-effort: the
# review itself, and every individual finding's own comment, is unaffected).
_MAX_FINDINGS_IN_PROMPT = 40


def _build_synthesis_prompt(issues: Sequence[CodeReviewIssue]) -> str:
    """Build the user prompt: every finding indexed, tagged with its cluster id.

    Preconditions: ``issues`` is non-empty.
    Postconditions:
        - Renders each finding via ``_render_finding_block`` (index, severity,
          category, location, description, suggestion), preceded by a
          ``group: g<N>`` line from :func:`group_similar_findings` clustering
          over ``issues`` — a hint the model may use, not a hard boundary.
        - Ends with the strict-JSON formatting instructions. Never raises.
    """
    groups = group_similar_findings(list(issues))
    group_of_id: Dict[int, int] = {}
    for group_idx, group in enumerate(groups):
        for finding in group:
            group_of_id[id(finding)] = group_idx

    parts: List[str] = [
        f"{len(issues)} confirmed, in-scope findings from one pull request review.",
        "A rough similarity-based grouping is given as a hint; the same "
        "underlying problem can also surface across different groups.",
        "",
    ]
    for i, issue in enumerate(issues):
        parts.append(f"group: g{group_of_id.get(id(issue), i)}")
        parts.extend(_render_finding_block(i, issue))
        parts.append("")
    parts.append(SYSTEMIC_SYNTHESIS_FORMATTING_INSTRUCTIONS)
    return "\n".join(parts)


def _location_for(issue: CodeReviewIssue) -> Dict[str, Any]:
    """Postconditions: ``{"file_path", "description"}`` for one finding, JSON-safe.

    ``description`` is token-scrubbed (mirrors ``proposal_from_findings``):
    this dict is persisted on ``review_summary`` and served through the Code
    Review API/UI, not just posted (already-scrubbed) as a GitHub comment.
    """
    return {
        "file_path": str(issue.file_path or ""),
        "description": scrub_token_from_text(str(issue.description or "")),
    }


def _parse_systemic_findings(
    data: object, issues: Sequence[CodeReviewIssue]
) -> List[Dict[str, Any]]:
    """Map a synthesis reply to persisted/postable systemic-finding dicts.

    Preconditions: ``issues`` is the exact finding list the prompt indexed.
    Postconditions:
        - Reads ``data["systemic_findings"]`` (missing/wrong type → ``[]``).
        - Each entry needs a non-blank ``title``, ``description``, and at
          least two valid, in-range, non-duplicate ``finding_indices``; an
          entry failing any of that is dropped rather than partially kept.
        - A kept entry's ``related_locations`` resolves each valid index back
          to :func:`_location_for` — no index leaks into the output. The
          entry's own ``title``/``description`` (model-generated text, not
          sourced from a single finding) are token-scrubbed the same way
          :func:`_location_for` scrubs each location's description: this
          result is persisted on ``review_summary`` and served through the
          Code Review API/UI, not just posted (already-scrubbed) as a GitHub
          comment. Malformed replies yield ``[]``. Pure; never raises.
    """
    if not isinstance(data, dict):
        return []
    raw = data.get("systemic_findings")
    if not isinstance(raw, list):
        return []
    n = len(issues)
    results: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "") or "").strip()
        description = str(item.get("description", "") or "").strip()
        if not title or not description:
            continue
        raw_indices = item.get("finding_indices")
        if not isinstance(raw_indices, list):
            continue
        seen: set[int] = set()
        locations: List[Dict[str, Any]] = []
        for raw_index in raw_indices:
            if isinstance(raw_index, bool) or not isinstance(raw_index, int):
                continue
            if not (0 <= raw_index < n) or raw_index in seen:
                continue
            seen.add(raw_index)
            locations.append(_location_for(issues[raw_index]))
        if len(locations) < MIN_FINDINGS_FOR_SYNTHESIS:
            continue
        results.append(
            {
                "title": scrub_token_from_text(title),
                "description": scrub_token_from_text(description),
                "related_locations": locations,
            }
        )
    return results


def synthesize_systemic_findings(
    issues: Sequence[CodeReviewIssue],
    *,
    llm: Optional[LLMClient] = None,
    input_data: Optional[CodeReviewInput] = None,
) -> List[Dict[str, Any]]:
    """Best-effort synthesis of cross-cutting patterns across in-scope findings.

    ``llm`` is supplied by the caller — the verification pipeline resolves the
    ``code_review_verify`` model and passes it in, mirroring
    ``scope_classifier.classify_scope``. This pass does not self-resolve a
    client; ``llm=None`` degrades to ``[]``.

    ``input_data`` is currently unused by the prompt itself (the findings'
    own text is the evidence), accepted for signature symmetry with
    ``classify_scope`` and to leave room for a future context-grounded
    version without changing the call site.

    Preconditions:
        - ``issues`` is a sequence of ``CodeReviewIssue``-like findings (each
          exposes ``file_path``/``line``/``severity``/``category``/
          ``description``/``suggestion``).

    Postconditions:
        - Returns ``[]`` when ``len(issues) < MIN_FINDINGS_FOR_SYNTHESIS``,
          when ``llm`` is ``None``/the unscripted dummy harness, or on any
          failure (LLM error, malformed reply).
        - Otherwise returns a list of ``{"title", "description",
          "related_locations"}`` dicts — see :func:`_parse_systemic_findings`
          for the per-entry validation. An empty list is also the normal,
          correct result when the model finds no genuine cross-cutting
          pattern.
        - At most :data:`_MAX_FINDINGS_IN_PROMPT` findings (a stable prefix of
          ``issues``) are offered to the synthesis prompt.
        - **Never raises**: client absence and any LLM/parse failure all
          degrade to ``[]`` rather than propagating.

    Side effects:
        - At most one ``complete_json`` LLM call, unless short-circuited above.
    """
    if len(issues) < MIN_FINDINGS_FOR_SYNTHESIS:
        return []
    if llm is None or is_unscripted_dummy(llm):
        return []

    prompt_issues = list(issues)[:_MAX_FINDINGS_IN_PROMPT]
    try:
        prompt = _build_synthesis_prompt(prompt_issues)
        # Route through the shared single-shot helper (plain-JSON mode, no
        # Pydantic schema) rather than hand-rolling complete_json — see
        # docs/LLM_CALLING_PATTERN_DECISION.md (no new Pattern-5 call sites).
        data = run_single_shot_review(
            llm,
            "code_review_verify",
            prompt,
            SYSTEMIC_SYNTHESIS_SYSTEM_PROMPT,
            objective="synthesize systemic PR review findings",
            temperature=0.0,
        )
        return _parse_systemic_findings(data, prompt_issues)
    except Exception as exc:  # noqa: BLE001 — best-effort pass, never raises
        logger.warning(
            "SystemicSynthesis: synthesis failed (%s: %s); skipping",
            type(exc).__name__,
            exc,
        )
        return []
