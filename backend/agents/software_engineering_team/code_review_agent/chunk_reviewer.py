"""Chunk Reviewer: the map step of the map-reduce code review.

``ChunkReviewAgent`` reviews exactly one ``ReviewChunk`` via a two-call
via-reasoning path (prose review with thinking, then schema-validated JSON
formatting with thinking off) and returns that chunk's findings. The coordinator
(``coordinator.py``) owns splitting the submission into bounded chunks,
recovering failed chunks, and reducing the per-chunk findings into one verdict;
this module owns only the single bounded review pass.

Preconditions:
    - The caller (the coordinator) has already bounded the chunk: the
      ``code_chunk`` carries at most ``compute_code_review_map_chunk_chars`` of
      code. ``architecture_overview`` and ``existing_codebase`` context are
      always passed through in full; the ``acceptance_criteria``/``spec_excerpt``
      blocks are included only when ``input_data.spec_compliance_single_pass`` is
      falsy (the coordinator sets it when ``CODE_REVIEW_SPEC_COMPLIANCE_PASS`` is
      enabled for the ``CODE_REVIEW`` profile, deferring spec-compliance findings
      to a single post-dedupe pass instead). This module never mutates the code
      under review.

Postconditions:
    - Returns the LLM's findings for this chunk only (``approved``, ``issues``,
      ``summary``, and the ``spec_compliance_notes`` passthrough); it never
      re-anchors line numbers, dedupes, or applies the approval gate — those are
      the reduce phase's job.

Invariants:
    - Stateless apart from the injected ``llm`` handle: every chunk review issues
      two LLM requests (call 1: ``llm.complete`` with thinking; call 2:
      ``llm.complete_json`` with thinking off, via
      ``complete_validated_via_reasoning_local``), so concurrent reviews share
      no mutable state. No strands ``Agent``/``Model`` is built for this call
      path. The code under review is sent verbatim and is never compacted.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional, Union

from llm_service import LLMClient
from llm_service.interface import observer_turn_started_monotonic

from .models import ChunkReviewInput, ChunkReviewLLMResponse, ChunkReviewOutput
from .profiles import (
    build_review_formatting_instructions,
    build_review_reasoning_system_prompt,
)
from .transcript import model_label, record_transcript_entry
from .via_reasoning import (
    complete_validated_via_reasoning_local,
    formatting_system_prompt_with_untrusted_guard,
)

logger = logging.getLogger(__name__)

# Extensions recognized as Python when a chunk's language isn't declared and
# must be guessed from its file path. Mirrors code_boundaries._PYTHON_EXTS.
_PYTHON_FILE_EXTS = (".py", ".pyi")

CHUNK_REVIEW_NOTE = "\n**Note:** This is one chunk of the full codebase. Review only the code below. Report issues with file_path set to the path provided for this chunk.\n"

# Guardrails that keep the reviewer from filing the false positives this engine
# was seeing. Injected into the per-chunk user prompt (NOT the system prompt) so
# the byte-locked CODE_REVIEW system prompt stays unchanged. Covers four
# recurring bad-comment patterns: phantom truncation, "add a file that exists",
# flagging conventional intra-package relative imports, and attempting a
# cross-caller impact check this bounded chunk has no tools to perform.
REVIEW_GUARDRAILS_NOTE = (
    "\n**Review guardrails (avoid these false positives):**\n"
    "- Surface-first: the code shown below is COMPLETE for what is displayed. Each function, "
    "method, class, and test is presented in full. Never report a function, test, or block as "
    "'truncated', 'cut off', or 'missing its body' based on where the shown code ends — the end "
    "of the shown code is not evidence of an incomplete implementation.\n"
    "- You are shown only the file(s) this change touched, not the whole repository. Do NOT claim "
    "that a file, module, or symbol referenced here 'does not exist', 'must be created', or 'needs "
    "to be added' SOLELY because it is off-chunk — an unchanged file or symbol may simply not be "
    "shown here. Only flag a genuinely broken reference you can substantiate from the code in "
    "front of you.\n"
    "- Do NOT verify whether this chunk's changes break callers elsewhere in the codebase — you "
    "have no tools to search beyond this chunk. Defer that cross-caller check to the dedicated "
    "side-effect / blast-radius pass, which runs once per submission with the tools to do it.\n"
    "- Intra-package relative imports (e.g. `from .models import X`, `from .store import Y`) are "
    "the established convention in this codebase and resolve to sibling modules in the same "
    "package. Do NOT flag them as unclear or ask to convert them to absolute imports.\n"
)

# Header that precedes the code block in every chunk-review prompt. Exposed as a
# named constant so callers/tests can identify a map-phase review prompt without
# duplicating the literal (it is unique to this prompt template).
CODE_TO_REVIEW_HEADER = "**Code to review:**"


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


def _build_shared_review_prefix(
    spec_excerpt: str,
    architecture_overview: str,
    existing_codebase_excerpt: str,
    spec_compliance_single_pass: bool,
) -> list[str]:
    """Render the map-phase prompt segment shared by every chunk in one run.

    Groups the three context blocks that are identical across every chunk of
    a coordinator run — the spec excerpt, the architecture overview, and the
    existing-codebase excerpt — into one contiguous run of prompt lines, with
    no per-chunk content interleaved between them. ``_run_chunk_review`` is
    the single call site that appends this segment to ``context_parts``,
    ahead of every per-chunk-varying block, so a future caching-aware prompt
    assembly has one obvious place to mark the joined result as a stable,
    cacheable prefix.

    Preconditions:
        - ``spec_excerpt``, ``architecture_overview``, and
          ``existing_codebase_excerpt`` are strings (each may be empty).
        - ``spec_compliance_single_pass`` mirrors
          ``ChunkReviewInput.spec_compliance_single_pass``: when true, the
          coordinator runs a dedicated post-dedupe spec-compliance pass
          instead (see ADR-010), so the spec-excerpt block is omitted here
          regardless of whether ``spec_excerpt`` is set.

    Postconditions:
        - Returns the prompt lines for whichever of the three blocks are
          present, in this fixed order: spec excerpt (only when
          ``spec_excerpt`` is truthy AND ``spec_compliance_single_pass`` is
          false), architecture overview (whenever ``architecture_overview``
          is truthy), existing-codebase excerpt (whenever
          ``existing_codebase_excerpt`` is truthy) — reusing the exact
          headers (``**Project specification (excerpt):**`` /
          ``**Architecture:**`` / ``**Existing codebase (excerpt):**``) and
          ``---`` delimiters this prompt has always used for these blocks.
        - Returns ``[]`` when all three blocks are absent/suppressed.
        - Never raises; never truncates or otherwise transforms the inputs
          (the coordinator has already bounded them).
    """
    parts: list[str] = []
    if spec_excerpt and not spec_compliance_single_pass:
        parts.extend(["", "**Project specification (excerpt):**", "---", spec_excerpt, "---"])
    if architecture_overview:
        parts.extend(["", "**Architecture:**", architecture_overview])
    if existing_codebase_excerpt:
        parts.extend(
            ["", "**Existing codebase (excerpt):**", "---", existing_codebase_excerpt, "---"]
        )
    return parts


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
        architecture/existing-codebase context and the sibling surface. The
        code, ``architecture_overview``, and ``existing_codebase`` fields are
        always passed through to the prompt in full; the
        ``acceptance_criteria``/``spec_excerpt`` blocks are included only when
        ``spec_compliance_single_pass`` is falsy (see module docstring).

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
          invokes the two-call via-reasoning path on the injected ``llm``, so
          concurrent ``run`` calls share no mutable state. The injected ``llm``
          must itself support concurrent calls — the central ``llm_service``
          clients do (they guard shared state internally); test doubles used
          with the parallel coordinator must do the same.
    """

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def run(
        self, input_data: ChunkReviewInput, think: Optional[Union[bool, str]] = None
    ) -> ChunkReviewOutput:
        """Review one chunk and return approved, issues, summary, and spec_compliance_notes.

        Preconditions:
            - ``think`` is ``None`` (defaults to max thinking on the reasoning
              pass) or an explicit override forwarded to the reasoning call —
              e.g. ``False`` for the coordinator's last-resort thinking-off
              retry of a chunk whose default-thinking review returned no usable
              content. The formatting pass always uses ``think=False``.

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
        - ``think`` is ``None`` (defaults to max thinking on the reasoning pass)
          or an explicit override for the reasoning call only; formatting always
          uses ``think=False``.

    Postconditions:
        - Shared context (spec/architecture/existing code) is passed through
          verbatim; this function never re-caps or re-compacts it — the
          coordinator's prep is the only place that bounds it, so a chunk call
          never fires extra LLM calls, but it also has no local defense if an
          upstream cap were ever skipped. Exception: when
          ``input_data.spec_compliance_single_pass`` is True, the
          ``acceptance_criteria``/``spec_excerpt`` blocks are omitted from the
          prompt entirely rather than passed through — the coordinator runs a
          dedicated post-dedupe spec-compliance pass instead (see ADR-010).
          ``architecture_overview`` and ``existing_codebase_excerpt`` are
          always passed through verbatim regardless of the flag. These three
          blocks are assembled as one contiguous segment (via
          ``_build_shared_review_prefix``) ahead of every per-chunk block
          (segment note, file/label, sibling surface, code chunk) in the
          composed prompt.
        - Buffers one ``chunk_review`` transcript entry (target
          ``input_data.file_path_or_label``) per LLM call the via-reasoning
          path makes: the reasoning ``complete`` call, then each
          ``complete_validated`` formatting attempt (the initial call plus
          every corrective retry), whether that attempt succeeded or failed —
          for later batched, off-hot-path persistence to
          ``code_review_transcripts``; see ``transcript.record_transcript_entry``
          and this function's ``on_attempt`` callback. A no-op when no
          ``job_id`` is bound on the current ``llm_attribution`` context (see
          ``CodeReviewAgent.run``); never raises and never blocks on I/O.

    Raises:
        LLMJsonParseError: the formatting pass could not produce parseable JSON
            on any of ``complete_validated``'s attempts (the initial call plus
            its corrective retries). The coordinator's recovery layer
            (``mapping.py``) classifies this as a recoverable content failure
            like any other malformed response.
        LLMSchemaValidationError: the formatting pass returned parseable JSON
            that fails ``ChunkReviewLLMResponse`` validation on every attempt —
            e.g. an out-of-set ``severity``/``category``, a non-strict-bool
            ``pre_existing``, a missing required top-level field, or an
            ``approved`` verdict inconsistent with its own issues list (see
            ``ChunkReviewLLMResponse._require_approval_consistent_with_issues``).
            Also classified as a recoverable content failure by ``mapping.py``.
        LLMSemanticExhaustionError: the reasoning pass produced no usable
            assistant content (a reasoning-only reply with no final answer).
            Propagates unchanged from ``complete_validated_via_reasoning_local``;
            also a recoverable content failure downstream.
        LLMTruncatedError: the reasoning or formatting reply hit the output-token
            limit (``finish_reason=length``). Propagates unchanged; also a
            recoverable content failure downstream — a smaller chunk yields a
            smaller review.
        LLMPermanentError: other unrecoverable LLM failures propagate unchanged.
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
    context_parts += [
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

    # Shared review prefix: identical across every chunk in this run. Kept
    # contiguous, with all per-chunk-varying content below, so this is the
    # single place a future caching-aware assembly would mark it as a stable
    # prefix (see _build_shared_review_prefix).
    context_parts.extend(
        _build_shared_review_prefix(
            spec_excerpt,
            architecture_overview,
            existing_codebase_excerpt,
            input_data.spec_compliance_single_pass,
        )
    )

    if input_data.segment_note:
        context_parts.extend(["**Segment notes:**", input_data.segment_note, ""])
    context_parts.append(f"**Files in this chunk:** {input_data.file_path_or_label}")
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
    context_parts.extend(
        [
            "",
            CODE_TO_REVIEW_HEADER,
            "```",
            code_chunk,
            "```",
        ]
    )

    prompt = "\n".join(context_parts)
    reasoning_system_prompt = build_review_reasoning_system_prompt(input_data.profile)
    formatting_system_prompt = formatting_system_prompt_with_untrusted_guard(None)
    model_name = model_label(llm)
    target = input_data.file_path_or_label
    last_attempt_start = time.monotonic()
    in_formatting = False

    def _on_formatting_start() -> None:
        nonlocal in_formatting
        in_formatting = True

    def _on_attempt(attempt_prompt: str, attempt_response: str) -> None:
        # One transcript entry per LLM HTTP turn: reasoning ``complete``
        # (including text continuations) then every formatting attempt.
        # Phase is stamped by ``on_formatting_start``, not callback index —
        # reasoning continuations must keep the reasoning system prompt.
        nonlocal last_attempt_start
        now = time.monotonic()
        started = observer_turn_started_monotonic()
        if started is None:
            started = last_attempt_start
        system_prompt = formatting_system_prompt if in_formatting else reasoning_system_prompt
        record_transcript_entry(
            "chunk_review",
            target,
            attempt_prompt,
            attempt_response,
            system_prompt=system_prompt,
            model=model_name,
            duration_ms=(now - started) * 1000,
            started_monotonic=started,
        )
        last_attempt_start = now

    response = complete_validated_via_reasoning_local(
        llm,
        schema=ChunkReviewLLMResponse,
        reasoning_prompt=prompt,
        reasoning_system_prompt=reasoning_system_prompt,
        formatting_instructions=build_review_formatting_instructions(input_data.profile),
        objective="review code chunk",
        reasoning_think=True if think is None else think,
        temperature=0.0,
        on_attempt=_on_attempt,
        on_formatting_start=_on_formatting_start,
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
