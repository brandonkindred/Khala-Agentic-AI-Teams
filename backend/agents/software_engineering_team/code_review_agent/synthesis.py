"""Reduce-phase synthesis: one findings-only LLM pass over a map-reduce review.

Stage 1 (the map-reduce coordinator) reviews a large submission in several
independent passes and then merges the results. The deterministic merge —
issue dedupe and the critical/high approval gate — is authoritative and lives
in ``coordinator.py``. This module owns only the *narrative*: a single cheap
LLM call that rewrites the per-pass summaries and spec notes into one coherent
report.

Two invariants hold for everything here:
    - The digest is built from findings only — issue metadata and per-pass
      summaries. Source code is never included.
    - Synthesis is best-effort and never authoritative. It cannot change the
      verdict or the issue list, and any failure returns ``None`` so the caller
      falls back to the deterministic concatenation behavior.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

from pydantic import BaseModel, Field
from strands import Agent

from llm_service import LLMClient

from .model_resolution import resolve_code_review_model
from .models import CodeReviewInput, CodeReviewIssue
from .prompts import REVIEW_SYNTHESIS_PROMPT

logger = logging.getLogger(__name__)

# Severity ordering for the digest, mirroring ``_VALID_SEVERITIES`` in
# coordinator.py. Lower rank prints first so the digest reads blocking-first.
# Unknown severities sort after every known one (stable within the bucket).
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_UNKNOWN_SEVERITY_RANK = len(_SEVERITY_RANK)


class SynthesisResult(BaseModel):
    """The narrative produced by one successful synthesis pass.

    Invariants:
        - Both fields are non-empty strings; ``synthesize_review_findings``
          returns ``None`` rather than a result with an empty field.
    """

    summary: str = Field(description="Unified review summary across all passes")
    spec_compliance_notes: str = Field(
        description="Unified spec/acceptance-criteria narrative across all passes"
    )


def build_findings_digest(
    issues: List[CodeReviewIssue],
    chunk_summaries: List[str],
    chunk_spec_notes: Optional[List[str]] = None,
) -> str:
    """Render findings as plain text for the synthesis prompt — never any code.

    Issues are ordered ``critical → high → medium → low → info`` (then unknown
    severities) purely so the digest reads blocking-first; this is presentation
    order, not truncation. Every issue, summary, and per-pass spec note is
    rendered in full — there are no length caps of any kind.

    The per-pass spec-compliance notes are included because the synthesized
    ``spec_compliance_notes`` *replaces* the concatenated per-pass notes
    downstream; without them in the digest the synthesizer would have to
    reconstruct acceptance-criteria observations from findings alone and could
    drop or contradict concrete evidence a reviewer already recorded.

    Preconditions:
        - ``issues`` is a list of ``CodeReviewIssue`` (may be empty).
        - ``chunk_summaries`` is a list of strings (may be empty); each is one
          per-pass summary.
        - ``chunk_spec_notes`` is None or a list of strings (may be empty); each
          is one per-pass spec-compliance note.

    Postconditions:
        - The returned text contains every issue, every non-empty summary, and
          every non-empty spec note in full, with no source code.
        - Ordering within the issue section is stable for equal severities
          (input order preserved).
    """
    ordered = sorted(
        enumerate(issues),
        key=lambda pair: (
            _SEVERITY_RANK.get((pair[1].severity or "").strip().lower(), _UNKNOWN_SEVERITY_RANK),
            pair[0],
        ),
    )

    lines: List[str] = ["## Findings"]
    if ordered:
        for _, issue in ordered:
            location = issue.file_path or "(file unknown)"
            if issue.line is not None:
                location = f"{location}:{issue.line}"
            parts = [f"- [{issue.severity}] {issue.category} {location} — {issue.description}"]
            if issue.suggestion:
                parts.append(f" (suggestion: {issue.suggestion})")
            lines.append("".join(parts))
    else:
        lines.append("- (no issues were flagged)")

    lines.append("")
    lines.append("## Per-pass summaries")
    non_empty_summaries = [s for s in chunk_summaries if s.strip()]
    if non_empty_summaries:
        for idx, summary in enumerate(non_empty_summaries, start=1):
            lines.append(f"### Pass {idx}")
            lines.append(summary)
    else:
        lines.append("(no per-pass summaries were produced)")

    lines.append("")
    lines.append("## Per-pass spec-compliance notes")
    non_empty_notes = [n for n in (chunk_spec_notes or []) if n.strip()]
    if non_empty_notes:
        for idx, note in enumerate(non_empty_notes, start=1):
            lines.append(f"### Pass {idx}")
            lines.append(note)
    else:
        lines.append("(no per-pass spec-compliance notes were produced)")

    return "\n".join(lines)


def synthesize_review_findings(
    llm: LLMClient,
    *,
    input_data: CodeReviewInput,
    approved: bool,
    issues: List[CodeReviewIssue],
    chunk_summaries: List[str],
    chunk_spec_notes: Optional[List[str]] = None,
) -> Optional[SynthesisResult]:
    """Run exactly one LLM pass to merge per-pass findings into one narrative.

    The model sees only the findings digest plus framing context (task, the
    deterministic verdict) — never source code. The strands model is resolved
    the same way as ``chunk_reviewer``: an injected ``llm`` is used directly
    when it implements the strands ``Model`` interface, otherwise the shared
    ``get_strands_model("code_review")`` is used.

    Preconditions:
        - ``llm`` is an ``LLMClient`` (and may also implement the strands
          ``Model`` interface, in which case it is used as the model directly).
        - ``approved`` is the deterministic verdict already decided by the
          coordinator; it is passed for context only and is never recomputed.
        - ``chunk_spec_notes`` is None or the per-pass spec-compliance notes;
          they are fed into the digest so the synthesized notes consolidate the
          reviewers' actual evidence rather than reconstructing it.

    Postconditions:
        - Returns a ``SynthesisResult`` with two non-empty strings on success.
        - Returns ``None`` on ANY failure — exception, malformed JSON, missing
          ``summary``/``spec_compliance_notes`` keys, or empty/whitespace-only
          values — so the caller falls back to deterministic concatenation.
        - Never raises, and never mutates ``issues`` or the verdict.
    """
    try:
        digest = build_findings_digest(issues, chunk_summaries, chunk_spec_notes)
        framing = _build_framing(input_data, approved)
        prompt = f"{framing}\n\n{digest}"

        _model = resolve_code_review_model(llm)
        agent = Agent(model=_model, system_prompt=REVIEW_SYNTHESIS_PROMPT)
        result = agent(prompt)
        data = json.loads(str(result).strip())

        if not isinstance(data, dict):
            logger.warning("ReviewSynthesis: model returned non-object JSON; falling back")
            return None

        summary = str(data.get("summary", "") or "").strip()
        spec_notes = str(data.get("spec_compliance_notes", "") or "").strip()
        if not summary or not spec_notes:
            logger.warning("ReviewSynthesis: missing/empty summary or spec notes; falling back")
            return None

        return SynthesisResult(summary=summary, spec_compliance_notes=spec_notes)
    except Exception as exc:  # noqa: BLE001 - best-effort cosmetic pass; never fail the review
        # Synthesis is purely narrative; any failure must fall back to the
        # deterministic concatenation rather than break an otherwise valid
        # review verdict, so the broad except here is the intended contract.
        logger.warning("ReviewSynthesis: synthesis failed (%s); falling back", exc)
        return None


def _build_framing(input_data: CodeReviewInput, approved: bool) -> str:
    """Render the non-code context lines that precede the findings digest.

    Postconditions:
        - The returned text carries the task description, acceptance criteria,
          and the deterministic verdict, and never includes any source code.
    """
    verdict = "approved" if approved else "rejected"
    lines = [f"The deterministic review verdict is: {verdict.upper()}."]
    if input_data.task_description.strip():
        lines.append(f"Task: {input_data.task_description.strip()}")
    if input_data.acceptance_criteria:
        lines.append("Acceptance criteria:")
        lines.extend(f"- {c}" for c in input_data.acceptance_criteria)
    return "\n".join(lines)
