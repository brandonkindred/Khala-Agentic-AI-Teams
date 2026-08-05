"""Chunk Reviewer: the map step of the map-reduce code review.

``ChunkReviewAgent`` reviews exactly one ``ReviewChunk`` in a single LLM call
and returns that chunk's findings. The coordinator (``coordinator.py``) owns
splitting the submission into bounded chunks, recovering failed chunks, and
reducing the per-chunk findings into one verdict; this module owns only the
single bounded review pass.

Preconditions:
    - The caller (the coordinator) has already bounded the chunk: the
      ``code_chunk`` carries at most ``compute_code_review_map_chunk_chars`` of
      code. Spec/architecture/existing-codebase context is passed through in
      full; this module never mutates the code under review.

Postconditions:
    - Returns the LLM's findings for this chunk only (``approved``, ``issues``,
      ``summary``, and the ``spec_compliance_notes`` passthrough); it never
      re-anchors line numbers, dedupes, or applies the approval gate — those are
      the reduce phase's job.

Invariants:
    - Stateless apart from the injected ``llm`` handle: every call goes
      straight to ``llm.complete_json`` (via ``complete_validated``), so
      concurrent reviews share no mutable state. No strands ``Agent``/``Model``
      is built for this call path. The code under review is sent verbatim and
      is never compacted.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional, Union

from llm_service import LLMClient, complete_validated

from .models import ChunkReviewInput, ChunkReviewLLMResponse, ChunkReviewOutput, ReviewProfile
from .profiles import build_review_system_prompt

logger = logging.getLogger(__name__)

# Extensions recognized as Python when a chunk's language isn't declared and
# must be guessed from its file path. Mirrors code_boundaries._PYTHON_EXTS.
_PYTHON_FILE_EXTS = (".py", ".pyi")

CHUNK_REVIEW_NOTE = "\n**Note:** This is one chunk of the full codebase. Review only the code below. Report issues with file_path set to the path provided for this chunk.\n"

# Guardrails that keep the reviewer from filing the false positives this engine
# was seeing. Injected into the per-chunk user prompt (NOT the system prompt) so
# the byte-locked CODE_REVIEW system prompt stays unchanged. Covers the three
# recurring bad-comment patterns: phantom truncation, "add a file that exists",
# and flagging conventional intra-package relative imports.
REVIEW_GUARDRAILS_NOTE = (
    "\n**Review guardrails (avoid these false positives):**\n"
    "- The code shown below is COMPLETE. Each function, method, class, and test is presented in "
    "full. Never report a function, test, or block as 'truncated', 'cut off', or 'missing its "
    "body' based on where the shown code ends — the end of the shown code is not evidence of an "
    "incomplete implementation.\n"
    "- You are shown only the file(s) this change touched, not the whole repository. Do NOT claim "
    "that a file, module, or symbol referenced here 'does not exist', 'must be created', or 'needs "
    "to be added' merely because it is not shown in this chunk — an unchanged file that already "
    "exists in the repo is simply not shown. Only flag a genuinely broken reference you can "
    "substantiate from the code in front of you.\n"
    "- Intra-package relative imports (e.g. `from .models import X`, `from .store import Y`) are "
    "the established convention in this codebase and resolve to sibling modules in the same "
    "package. Do NOT flag them as unclear or ask to convert them to absolute imports.\n"
)

# Header that precedes the code block in every chunk-review prompt. Exposed as a
# named constant so callers/tests can identify a map-phase review prompt without
# duplicating the literal (it is unique to this prompt template).
CODE_TO_REVIEW_HEADER = "**Code to review:**"

# Output-contract reminder appended AFTER the code block (the last thing the
# model reads). Keeps a thinking model from returning reasoning-only output with
# no final answer — the semantic-exhaustion failure mode. Rides the user prompt
# because the CODE_REVIEW system prompt is byte-locked.
FINAL_OUTPUT_CONTRACT_NOTE = (
    "\nRespond with ONLY the single JSON object your instructions specify "
    "(approved, issues, summary, spec_compliance_notes). "
    "Do not emit reasoning, analysis, or any prose outside that JSON object."
)


def _guess_language_from_label(file_path_or_label: str) -> Optional[str]:
    """Guess a chunk's language from the extension of its first file path.

    ``file_path_or_label`` may join several paths with ", " and mark partial
    segments with a trailing " (lines A-B of N)" — this looks only at the
    first path's extension.

    Postconditions:
        - Returns ``"python"`` for a ``.py``/``.pyi`` path; ``None`` otherwise
          (including an empty or path-less label), so callers can apply their
          own default.
    """
    first = file_path_or_label.split(",", 1)[0].split(" (", 1)[0].strip()
    ext = os.path.splitext(first)[1].lower()
    return "python" if ext in _PYTHON_FILE_EXTS else None


class ChunkReviewAgent:
    """The map step of the map-reduce code review: review exactly one chunk.

    How it is used:
        The public entry point ``coordinator.run_coordinator`` splits a
        submission into bounded ``ReviewChunk``s and, in its map phase, calls
        ``run`` once per chunk (in parallel), then reduces the per-chunk results
        into one verdict. Callers do not construct the prompt themselves::

            agent = ChunkReviewAgent(llm)
            out = agent.run(ChunkReviewInput(code_chunk=chunk.content, ...))
            # out.approved, out.issues, out.summary, ...

    Input (``ChunkReviewInput``):
        ``code_chunk`` is the rendered chunk (one or more files, already sized to
        the model's context by the coordinator) plus optional task/spec/
        architecture/existing-codebase context and the sibling surface. The code
        and all context fields are passed through to the prompt in full.

    Output (``ChunkReviewOutput``):
        This chunk's findings only — ``approved`` (no critical/high issues),
        ``issues`` (raw dicts the coordinator normalizes and re-anchors),
        ``summary``, and the ``spec_compliance_notes`` passthrough. It never
        re-anchors line numbers, dedupes, or applies the approval gate — those
        are the coordinator's reduce phase.

    Constraints:
        - Reviews a single chunk, not the whole codebase; cross-chunk and
          whole-submission concerns (dedupe, false-positive verification, final
          verdict) belong to the coordinator.
        - The caller must have bounded ``code_chunk`` to the map budget; this
          agent does not truncate the code or any context field.

    Invariants:
        - Stateless apart from the injected ``llm`` handle: every ``run`` call
          invokes the injected ``llm``'s own ``complete_json`` directly (via
          ``complete_validated``), so concurrent ``run`` calls share no mutable
          state. The injected ``llm`` must itself support concurrent calls —
          the central ``llm_service`` clients do (they guard shared state
          internally); test doubles used with the parallel coordinator must do
          the same.
    """

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def run(
        self, input_data: ChunkReviewInput, think: Optional[Union[bool, str]] = None
    ) -> ChunkReviewOutput:
        """Review one chunk and return approved, issues, summary, and spec_compliance_notes.

        Preconditions:
            - ``think`` is ``None`` (use the client's default thinking level) or an
              explicit override forwarded verbatim to ``complete_validated``/
              ``llm.complete_json`` — e.g. ``False`` for the coordinator's
              last-resort thinking-off retry of a chunk whose default-thinking
              review returned no usable content.

        Postconditions:
            - ``spec_compliance_notes`` from the LLM is passed through so
              single-chunk reviews keep full output fidelity.
        """
        result = _run_chunk_review(self.llm, input_data, think=think)
        return ChunkReviewOutput(
            approved=result["approved"],
            issues=result["issues"],
            summary=result["summary"],
            spec_compliance_notes=result["spec_compliance_notes"],
        )


def _run_chunk_review(
    llm: LLMClient, input_data: ChunkReviewInput, think: Optional[Union[bool, str]] = None
) -> dict:
    """
    Review one chunk of code. Returns dict with approved, issues, summary,
    and spec_compliance_notes.

    Preconditions:
        - ``input_data.code_chunk`` is already bounded by the coordinator
          (≤ ``compute_code_review_map_chunk_chars``); it is reviewed verbatim,
          never compacted or truncated here.
        - ``think`` is ``None`` (client default) or an explicit thinking
          override forwarded verbatim to ``complete_validated``/
          ``llm.complete_json``.

    Postconditions:
        - Shared context (spec/architecture/existing code) is passed through
          verbatim; this function never re-caps or re-compacts it — the
          coordinator's prep is the only place that bounds it, so a chunk call
          never fires extra LLM calls, but it also has no local defense if an
          upstream cap were ever skipped.

    Raises:
        LLMJsonParseError: the injected ``llm``'s ``complete_json`` could not
            produce parseable JSON on any of ``complete_validated``'s attempts
            (the initial call plus its corrective retries). The coordinator's
            recovery layer (``mapping.py``) classifies this as a recoverable
            content failure like any other malformed response.
        LLMSchemaValidationError: the LLM returned parseable JSON that fails
            ``ChunkReviewLLMResponse`` validation on every attempt — e.g. an
            out-of-set ``severity``/``category``, a non-strict-bool
            ``pre_existing``, a missing required top-level field, or an
            ``approved`` verdict inconsistent with its own issues list (see
            ``ChunkReviewLLMResponse._require_approval_consistent_with_issues``).
            Also classified as a recoverable content failure by ``mapping.py``.
        LLMSemanticExhaustionError: the model produced no usable assistant
            content (a reasoning-only reply with no final answer). Propagates
            unchanged from ``complete_validated``; also a recoverable content
            failure downstream.
        LLMTruncatedError: the reply hit the output-token limit
            (``finish_reason=length``). Propagates unchanged from
            ``complete_validated``; also a recoverable content failure
            downstream — a smaller chunk yields a smaller review.
        LLMPermanentError: other unrecoverable LLM failures propagate
            unchanged from ``complete_validated``.
    """
    code_chunk = input_data.code_chunk
    spec_excerpt = input_data.spec_excerpt
    architecture_overview = input_data.architecture_overview
    existing_codebase_excerpt = input_data.existing_codebase_excerpt or ""

    language = input_data.language.strip().lower() if input_data.language else ""
    if not language:
        # Fallback for legacy callers that did not declare a language: derive
        # it from the chunk's file extension rather than guessing from content.
        language = _guess_language_from_label(input_data.file_path_or_label) or "typescript"

    context_parts = [CHUNK_REVIEW_NOTE, REVIEW_GUARDRAILS_NOTE]
    if input_data.segment_note:
        context_parts.extend(["**Segment notes:**", input_data.segment_note, ""])
    context_parts += [
        f"**Files in this chunk:** {input_data.file_path_or_label}",
        f"**Language:** {language}",
        f"**Task description:** {input_data.task_description}",
    ]
    if input_data.task_requirements:
        context_parts.extend(["", "**Task requirements:**", input_data.task_requirements])
    if input_data.acceptance_criteria and not input_data.spec_compliance_single_pass:
        context_parts.extend(
            [
                "",
                "**Acceptance criteria (code MUST meet all of these):**",
                *[f"- {c}" for c in input_data.acceptance_criteria],
            ]
        )
    if input_data.user_decisions:
        context_parts.extend(
            [
                "",
                "**User decisions already made (settled — do NOT flag these as open/unanswered "
                "questions or suggest reconsidering them):**",
                *[f"- {d}" for d in input_data.user_decisions],
            ]
        )
    if spec_excerpt and not input_data.spec_compliance_single_pass:
        context_parts.extend(
            [
                "",
                "**Project specification (excerpt):**",
                "---",
                spec_excerpt,
                "---",
            ]
        )
    if architecture_overview:
        context_parts.extend(["", "**Architecture:**", architecture_overview])
    sibling_surface = input_data.sibling_surface or ""
    if sibling_surface:
        context_parts.extend(
            [
                "",
                "**Other files changed in this submission (top-level symbols they define/export):**",
                "Flag any reference in the code below to a symbol that a sibling file was "
                "expected to provide but no longer does (e.g. a renamed or removed function, "
                "class, or export). Do not flag symbols that are still present.",
                "---",
                sibling_surface,
                "---",
            ]
        )
    if existing_codebase_excerpt:
        context_parts.extend(
            [
                "",
                "**Existing codebase (excerpt):**",
                "---",
                existing_codebase_excerpt,
                "---",
            ]
        )
    context_parts.extend(
        [
            "",
            CODE_TO_REVIEW_HEADER,
            "```",
            code_chunk,
            "```",
            # Last thing the model sees: a plain output-contract reminder. The
            # CODE_REVIEW system prompt is byte-locked, so this rides the user
            # prompt (like REVIEW_GUARDRAILS_NOTE). It nudges a thinking model to
            # emit the final JSON rather than reasoning-only output, the failure
            # mode that otherwise raises LLMSemanticExhaustionError.
            FINAL_OUTPUT_CONTRACT_NOTE,
        ]
    )

    prompt = "\n".join(context_parts)
    response = complete_validated(
        llm,
        prompt,
        schema=ChunkReviewLLMResponse,
        objective="review code chunk",
        system_prompt=build_review_system_prompt(input_data.profile),
        temperature=0.0,
        think=think,
    )

    # Issue dicts are passed through raw: normalization (defaults, line
    # coercion, path resolution) happens exactly once, in the coordinator's
    # ``_issues_from_chunk_output``. A blank file_path is deliberately kept
    # blank — fabricating the multi-file chunk label here would break the
    # coordinator's per-path offset lookup.
    return {
        "approved": response.approved,
        "issues": [issue.model_dump() for issue in response.issues],
        "summary": response.summary,
        "spec_compliance_notes": response.spec_compliance_notes,
    }


def review_chunk(
    llm: LLMClient,
    code_chunk: str,
    file_paths_label: str,
    task_description: str,
    task_requirements: str,
    acceptance_criteria: List[str],
    spec_excerpt: str,
    architecture_overview: str,
    existing_codebase_excerpt: Optional[str],
    *,
    profile: ReviewProfile = ReviewProfile.CODE_REVIEW,
    language: str = "",
    segment_note: str = "",
    user_decisions: Optional[List[str]] = None,
    sibling_surface: str = "",
    think: Optional[Union[bool, str]] = None,
) -> dict:
    """Legacy function: review one chunk. Prefer ChunkReviewAgent.run(ChunkReviewInput(...))."""
    inp = ChunkReviewInput(
        code_chunk=code_chunk,
        file_path_or_label=file_paths_label,
        task_description=task_description,
        task_requirements=task_requirements,
        acceptance_criteria=acceptance_criteria,
        spec_excerpt=spec_excerpt,
        architecture_overview=architecture_overview,
        existing_codebase_excerpt=existing_codebase_excerpt,
        profile=profile,
        language=language,
        segment_note=segment_note,
        user_decisions=user_decisions,
        sibling_surface=sibling_surface,
    )
    result = _run_chunk_review(llm, inp, think=think)
    return result
