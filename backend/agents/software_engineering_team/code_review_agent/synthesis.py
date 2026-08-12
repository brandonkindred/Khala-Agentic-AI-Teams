"""Reduce-phase synthesis: findings-only LLM passes over a map-reduce review.

Stage 1 (the map-reduce coordinator) reviews a large submission in several
independent passes and then merges the results. The deterministic merge —
issue dedupe and the critical/high approval gate — is authoritative and lives
in ``coordinator.py``. This module owns only *narrative*, additive passes over
that merged result:

    - :func:`synthesize_review_findings` — a single cheap LLM call that
      rewrites the per-pass summaries and spec notes into one coherent report.
    - :func:`synthesize_spec_compliance` — a single dedicated LLM call that
      checks the merged findings against the FULL spec/acceptance-criteria
      text in one pass, instead of that text being repeated in every chunk's
      prompt. Not yet wired into the coordinator (see its own docstring).

Two invariants hold for everything here:
    - Every digest is built from findings only — issue metadata and per-pass
      summaries/notes. Source code is never included.
    - Every pass here is best-effort and never authoritative. None can change
      the verdict or the issue list, and any failure returns ``None`` so the
      caller can fall back to whatever it used before that pass existed.
"""

from __future__ import annotations

import json
import logging
import time
from typing import List, Optional

from pydantic import BaseModel, Field
from strands import Agent

from llm_service import LLMClient

from .model_resolution import resolve_code_review_model
from .models import CodeReviewInput, CodeReviewIssue
from .prompts import REVIEW_SYNTHESIS_PROMPT, SPEC_COMPLIANCE_PASS_PROMPT
from .transcript import model_label, record_transcript_entry

logger = logging.getLogger(__name__)

# Severity ordering for the digest, mirroring ``_VALID_SEVERITIES`` in
# coordinator.py. Lower rank prints first so the digest reads blocking-first.
# Unknown severities sort after every known one (stable within the bucket).
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_UNKNOWN_SEVERITY_RANK = len(_SEVERITY_RANK)


class SynthesisResult(BaseModel):
    """The narrative produced by one successful synthesis pass.

    Invariants:
        - ``summary`` is a non-empty string; ``synthesize_review_findings``
          returns ``None`` rather than a result with an empty ``summary``.
        - ``spec_compliance_notes`` may be empty: an empty string means the
          reviewers recorded no spec/acceptance-criteria gaps, and downstream
          rendering omits the spec-compliance section entirely.
    """

    summary: str = Field(description="Unified review summary across all passes")
    spec_compliance_notes: str = Field(
        default="",
        description="Consolidated spec/acceptance-criteria gaps, or '' when there are none",
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
        - Records one transcript entry (stage ``synthesis``) for the LLM call
          once a response is received — the raw response text, whether or not
          it parses or validates, so a malformed response is visible for
          debugging rather than silently discarded.
        - Returns a ``SynthesisResult`` with a non-empty ``summary`` on success;
          ``spec_compliance_notes`` may be empty (no spec gaps were recorded).
        - Returns ``None`` on ANY failure — exception, malformed JSON, a missing
          ``summary`` key, or an empty/whitespace-only ``summary`` — so the caller
          falls back to deterministic concatenation.
        - Never raises, and never mutates ``issues`` or the verdict.
    """
    try:
        digest = build_findings_digest(issues, chunk_summaries, chunk_spec_notes)
        framing = _build_framing(input_data, approved)
        prompt = f"{framing}\n\n{digest}"

        _model = resolve_code_review_model(llm)
        agent = Agent(model=_model, system_prompt=REVIEW_SYNTHESIS_PROMPT)
        started = time.monotonic()
        result = agent(prompt)
        raw = str(result).strip()
        record_transcript_entry(
            "synthesis",
            "",
            prompt,
            raw,
            model=model_label(_model),
            duration_ms=(time.monotonic() - started) * 1000,
        )
        data = json.loads(raw)

        if not isinstance(data, dict):
            logger.warning("ReviewSynthesis: model returned non-object JSON; falling back")
            return None

        summary = str(data.get("summary", "") or "").strip()
        spec_notes = str(data.get("spec_compliance_notes", "") or "").strip()
        if not summary:
            logger.warning("ReviewSynthesis: missing/empty summary; falling back")
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


def _build_spec_compliance_framing(input_data: CodeReviewInput) -> str:
    """Render the non-code context lines for the dedicated spec-compliance pass.

    Unlike :func:`_build_framing`, this includes the FULL, uncompacted
    ``spec_content`` in addition to the acceptance criteria: this pass runs
    exactly once per submission, so the fidelity a per-chunk prompt could not
    afford (without repeating the same text once per chunk) is affordable here.

    Postconditions:
        - The returned text carries the task description, the full acceptance
          criteria, and the full spec content (each only when non-blank), and
          never includes any source code or findings.
    """
    lines: List[str] = []
    if input_data.task_description.strip():
        lines.append(f"Task: {input_data.task_description.strip()}")
    if input_data.acceptance_criteria:
        lines.append("Acceptance criteria (code MUST meet all of these):")
        lines.extend(f"- {c}" for c in input_data.acceptance_criteria)
    if input_data.spec_content.strip():
        lines.extend(["", "Project specification (full):", "---", input_data.spec_content, "---"])
    return "\n".join(lines)


def synthesize_spec_compliance(
    llm: LLMClient,
    *,
    input_data: CodeReviewInput,
    issues: List[CodeReviewIssue],
) -> Optional[str]:
    """Run exactly one LLM pass to check spec/acceptance-criteria compliance.

    Paired with :func:`synthesize_review_findings`: the same single-call,
    findings-only, fail-safe shape, but scoped purely to spec/acceptance-
    criteria compliance and given the FULL spec/acceptance-criteria text
    (never compacted, and read once here rather than once per chunk) instead
    of per-chunk-sourced spec notes.

    Preconditions:
        - ``llm`` is an ``LLMClient`` (and may also implement the strands
          ``Model`` interface, in which case it is used as the model directly).
        - ``issues`` is the final merged/deduped issue list for this
          submission (post map-reduce, post any tail passes) — never raw,
          unmerged per-chunk output.

    Postconditions:
        - Records one transcript entry (stage ``spec_compliance``) for the LLM
          call once a response is received — the raw response text, whether or
          not it parses or validates, so a malformed response is visible for
          debugging rather than silently discarded.
        - Returns a string on success: ``""`` when no gaps were found, or the
          consolidated spec/acceptance-criteria gaps otherwise — the same
          shape as ``CodeReviewOutput.spec_compliance_notes``.
        - Returns ``None`` on ANY failure — exception, malformed JSON, a
          non-object response, or a response missing the
          ``spec_compliance_notes`` key entirely — so the caller can treat
          this pass as unavailable and fall back accordingly.
        - Never raises, and never mutates ``issues``.
    """
    try:
        digest = build_findings_digest(issues, [])
        framing = _build_spec_compliance_framing(input_data)
        prompt = f"{framing}\n\n{digest}"

        _model = resolve_code_review_model(llm)
        agent = Agent(model=_model, system_prompt=SPEC_COMPLIANCE_PASS_PROMPT)
        started = time.monotonic()
        result = agent(prompt)
        raw = str(result).strip()
        record_transcript_entry(
            "spec_compliance",
            "",
            prompt,
            raw,
            model=model_label(_model),
            duration_ms=(time.monotonic() - started) * 1000,
        )
        data = json.loads(raw)

        if not isinstance(data, dict):
            logger.warning("SpecCompliancePass: model returned non-object JSON; skipping")
            return None
        if "spec_compliance_notes" not in data:
            logger.warning("SpecCompliancePass: missing spec_compliance_notes key; skipping")
            return None

        return str(data.get("spec_compliance_notes", "") or "").strip()
    except Exception as exc:  # noqa: BLE001 - best-effort pass; never fail the review
        # This pass is purely additive narrative, same contract as
        # synthesize_review_findings: any failure must leave the caller free
        # to fall back rather than break an otherwise valid review.
        logger.warning("SpecCompliancePass: pass failed (%s); skipping", exc)
        return None
