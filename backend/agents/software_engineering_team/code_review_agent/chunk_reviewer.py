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
    - Stateless apart from the injected ``llm`` handle: every chunk review
      issues two LLM requests via ``run_agent_via_reasoning`` — call 1 runs a
      text-mode Strands ``Agent`` with thinking; call 2 is
      ``llm.complete_json`` with thinking off — so concurrent reviews share no
      mutable state. The shared spec/architecture/existing-code prefix (see
      ``_build_shared_review_prefix``), when present, is attached to call 1's
      ``Agent`` as a ``CacheBreakpoint``-marked system-content segment rather
      than embedded in the user-turn prompt. The code under review is sent
      verbatim and is never compacted.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional, Union

from pydantic import ValidationError

from llm_service import CacheBreakpoint, LLMClient, LLMSchemaValidationError
from llm_service.interface import take_complete_json_turns
from software_engineering_team.shared.json_utils import parse_json_object

from .model_resolution import resolve_code_review_model
from .models import ChunkReviewInput, ChunkReviewLLMResponse, ChunkReviewOutput
from .profiles import (
    build_review_formatting_instructions,
    build_review_reasoning_system_prompt,
)
from .transcript import (
    model_label,
    record_formatting_transcript_turns,
    record_reasoning_transcript_turns,
    record_transcript_entry,
    resolve_format_turn_started,
)
from .via_reasoning import formatting_system_prompt_with_untrusted_guard, run_agent_via_reasoning

logger = logging.getLogger(__name__)

# Extensions recognized as Python when a chunk's language isn't declared and
# must be guessed from its file path. Mirrors code_boundaries._PYTHON_EXTS.
_PYTHON_FILE_EXTS = (".py", ".pyi")

CHUNK_REVIEW_NOTE = "\n**Note:** This is one chunk of the full codebase. Review only the code shown for this chunk. Report issues with file_path set to the path provided for this chunk.\n"

# Guardrails that keep the reviewer from filing the false positives this engine
# was seeing. Injected into the per-chunk user prompt (NOT the system prompt) so
# the byte-locked CODE_REVIEW system prompt stays unchanged. Covers four
# recurring bad-comment patterns: phantom truncation, "add a file that exists",
# flagging conventional intra-package relative imports, and attempting a
# cross-caller impact check this bounded chunk has no tools to perform.
REVIEW_GUARDRAILS_NOTE = (
    "\n**Review guardrails (avoid these false positives):**\n"
    "- Surface-first: the code shown for this chunk is COMPLETE for what is displayed. Each function, "
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
    the single call site: it joins this segment into a single
    ``CacheBreakpoint``-marked system-content entry attached to the reasoning
    ``Agent`` (see ``run_agent_via_reasoning``'s ``system_prompt_content``),
    kept entirely separate from the per-chunk-varying user-turn prompt, so
    per-chunk map calls stop re-billing this shared prefix.

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


def _build_chunk_file_context_prefix(input_data: ChunkReviewInput) -> list[str]:
    """Render the microtask file context this chunk carries, as a stable prefix.

    Groups the content that identifies and shows the code under review — the
    segment note (if any), the "Files in this chunk" label, the sibling-file
    surface (if any), and finally the code block itself — into one contiguous
    run of prompt lines, positioned ahead of the per-chunk role instructions
    built by ``_build_chunk_role_instructions``. This is a pure isolation/
    reorder of content ``_run_chunk_review`` already built inline; no content
    is added, removed, or reworded here beyond what its callers already sent
    (aside from the "below"/"for this chunk" wording fixes in
    ``CHUNK_REVIEW_NOTE``/``REVIEW_GUARDRAILS_NOTE`` that this reorder required).

    Preconditions:
        - ``input_data`` is a valid ``ChunkReviewInput`` (``code_chunk`` set).

    Postconditions:
        - Returns non-empty prompt lines ending with the code fence around
          ``input_data.code_chunk``, preceded by the segment note (only when
          ``input_data.segment_note`` is truthy), the file-path label, and
          the sibling-surface block (only when ``input_data.sibling_surface``
          is truthy) — in that fixed order.
        - Never raises; never truncates or otherwise transforms the inputs.
    """
    parts: list[str] = []
    if input_data.segment_note:
        parts.extend(["**Segment notes:**", input_data.segment_note, ""])
    parts.append(f"**Files in this chunk:** {input_data.file_path_or_label}")
    sibling_surface = input_data.sibling_surface or ""
    if sibling_surface:
        parts.extend(
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
    parts.extend(
        [
            "",
            CODE_TO_REVIEW_HEADER,
            "```",
            input_data.code_chunk,
            "```",
        ]
    )
    return parts


def _build_chunk_role_instructions(input_data: ChunkReviewInput, language: str) -> list[str]:
    """Render the per-chunk role-specific review instructions.

    Groups the reviewer guardrails, task framing, and settled decisions that
    are NOT part of the shared file-context prefix — these are placed after
    ``_build_chunk_file_context_prefix``'s output in the assembled prompt
    (reorder/isolation only; no cache marking here).

    Preconditions:
        - ``input_data`` is a valid ``ChunkReviewInput``.
        - ``language`` is the resolved (non-empty) language label for this chunk.

    Postconditions:
        - Returns non-empty prompt lines: ``CHUNK_REVIEW_NOTE``,
          ``REVIEW_GUARDRAILS_NOTE``, the language line, and the task
          description, followed by task requirements, acceptance criteria,
          and user decisions when each is present — in that fixed order.
        - Never raises; never truncates or otherwise transforms the inputs.
    """
    parts = [
        CHUNK_REVIEW_NOTE,
        REVIEW_GUARDRAILS_NOTE,
        f"**Language:** {language}",
        f"**Task description:** {input_data.task_description}",
    ]
    if input_data.task_requirements:
        parts.extend(["", "**Task requirements:**", input_data.task_requirements])
    if input_data.acceptance_criteria and not input_data.spec_compliance_single_pass:
        parts.extend(
            [
                "",
                "**Acceptance criteria (code MUST meet all of these):**",
                *[f"- {c}" for c in input_data.acceptance_criteria],
            ]
        )
    if input_data.user_decisions:
        parts.extend(
            [
                "",
                "**User decisions already made (settled — do NOT flag these as open/unanswered "
                "questions or suggest reconsidering them):**",
                *[f"- {d}" for d in input_data.user_decisions],
            ]
        )
    return parts


def _parse_chunk_review_response(raw: str) -> ChunkReviewLLMResponse:
    """Parse and validate one chunk-review formatting-pass reply.

    Preconditions:
        - ``raw`` is the JSON text from ``run_agent_via_reasoning``'s
          formatting pass (``json.dumps`` of whatever ``complete_json``
          returned).

    Postconditions:
        - Returns a validated ``ChunkReviewLLMResponse``.
        - ``LLMJsonParseError`` propagates unchanged from ``parse_json_object``
          when ``raw`` contains no recoverable JSON object.
        - Raises ``LLMSchemaValidationError`` — never a bare
          ``pydantic.ValidationError`` — both when the recovered JSON is not
          an object (``parse_json_object`` raises ``TypeError`` for that
          shape) and when the parsed dict fails ``ChunkReviewLLMResponse``
          validation. This exact exception type matters: ``mapping.py``'s
          chunk-recovery classification pattern-matches on
          ``(LLMJsonParseError, LLMSchemaValidationError)``.
        - No local corrective retry — ``run_agent_via_reasoning`` is
          single-shot; the coordinator's chunk-level recovery
          (``mapping.py``) is the retry layer for a rejected reply.
    """
    try:
        data = parse_json_object(raw)
    except TypeError as exc:
        raise LLMSchemaValidationError(
            f"chunk review formatting pass returned non-object JSON: {exc}",
            response_preview=raw[:500],
            cause=exc,
        ) from exc
    try:
        return ChunkReviewLLMResponse.model_validate(data)
    except ValidationError as exc:
        raise LLMSchemaValidationError(
            f"chunk review formatting pass failed ChunkReviewLLMResponse validation: {exc}",
            response_preview=raw[:500],
            cause=exc,
        ) from exc


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
          blocks (via ``_build_shared_review_prefix``) are attached to the
          reasoning ``Agent``'s system content as a single
          ``CacheBreakpoint``-marked segment — not embedded in the user-turn
          prompt — whenever at least one is present; when all three are
          absent, no system-content segment is attached and behavior is
          unchanged from before this cache-breakpoint mechanism existed.
        - Buffers one ``chunk_review`` transcript entry (target
          ``input_data.file_path_or_label``) per LLM call ``run_agent_via_reasoning``
          makes: the reasoning ``Agent`` invocation, then the single
          formatting ``complete_json`` call — for later batched,
          off-hot-path persistence to ``code_review_transcripts``; see
          ``transcript.record_reasoning_transcript_turns`` /
          ``record_formatting_transcript_turns``. A no-op when no ``job_id``
          is bound on the current ``llm_attribution`` context (see
          ``CodeReviewAgent.run``); never raises and never blocks on I/O.

    Raises:
        LLMJsonParseError: the formatting pass could not produce parseable JSON.
            The coordinator's recovery layer (``mapping.py``) classifies this
            as a recoverable content failure like any other malformed response.
        LLMSchemaValidationError: the formatting pass returned parseable JSON
            that fails ``ChunkReviewLLMResponse`` validation — e.g. an
            out-of-set ``severity``/``category``, a non-strict-bool
            ``pre_existing``, a missing required top-level field, or an
            ``approved`` verdict inconsistent with its own issues list (see
            ``ChunkReviewLLMResponse._require_approval_consistent_with_issues``),
            or non-object JSON. Raised by ``_parse_chunk_review_response``.
            No local corrective retry: also classified as a recoverable
            content failure by ``mapping.py``, whose chunk-level retry is now
            the sole recovery layer for a rejected reply.
        LLMSemanticExhaustionError: the reasoning pass produced no usable
            assistant content (a reasoning-only reply with no final answer).
            Propagates unchanged from ``run_agent_via_reasoning``; also a
            recoverable content failure downstream.
        LLMTruncatedError: the reasoning or formatting reply hit the output-token
            limit (``finish_reason=length``). Propagates unchanged; also a
            recoverable content failure downstream — a smaller chunk yields a
            smaller review.
        LLMPermanentError: other unrecoverable LLM failures propagate unchanged.
    """
    spec_excerpt = input_data.spec_excerpt
    architecture_overview = input_data.architecture_overview
    existing_codebase_excerpt = input_data.existing_codebase_excerpt or ""

    language = input_data.language.strip().lower() if input_data.language else ""
    if not language:
        # Fallback for legacy callers that did not declare a language: derive
        # it from the chunk's file extension rather than guessing from content.
        language = _guess_language_from_label(input_data.file_path_or_label) or "typescript"

    # Shared review prefix: identical across every chunk in this run. Attached
    # to the reasoning Agent's system content as a CacheBreakpoint-marked
    # segment (below) rather than embedded here, so per-chunk map calls stop
    # re-billing it.
    shared_parts = _build_shared_review_prefix(
        spec_excerpt,
        architecture_overview,
        existing_codebase_excerpt,
        input_data.spec_compliance_single_pass,
    )
    system_prompt_content = [CacheBreakpoint("\n".join(shared_parts))] if shared_parts else None

    # Microtask file context (this chunk's code) as a stable prefix, ahead of
    # the per-chunk role-specific instructions.
    context_parts = _build_chunk_file_context_prefix(input_data)
    context_parts += _build_chunk_role_instructions(input_data, language)

    prompt = "\n".join(context_parts)
    reasoning_system_prompt = build_review_reasoning_system_prompt(input_data.profile)
    formatting_system_prompt = formatting_system_prompt_with_untrusted_guard(None)
    model = resolve_code_review_model(llm)
    model_name = model_label(model)
    target = input_data.file_path_or_label

    reasoning_agent = None
    reasoning_turns: list[tuple[str, str, float]] = []
    format_turns: list[tuple[str, str, float]] = []
    started = time.monotonic()
    reasoning_done_at = started
    format_turn_started_at: Optional[float] = None

    def _capture(agent: object) -> None:
        nonlocal reasoning_agent, reasoning_done_at, reasoning_turns
        reasoning_agent = agent
        reasoning_done_at = time.monotonic()
        reasoning_turns = take_complete_json_turns()

    def _on_formatting_start() -> None:
        nonlocal format_turn_started_at
        format_turn_started_at = time.monotonic()

    def _capture_formatting(format_prompt: str, format_response: str) -> None:
        turn_started = resolve_format_turn_started(
            [turn_started for _, _, turn_started in format_turns],
            format_turn_started_at,
            time.monotonic(),
        )
        format_turns.append((format_prompt, format_response, turn_started))

    try:
        response = run_agent_via_reasoning(
            model=model,
            reasoning_prompt=prompt,
            reasoning_system_prompt=reasoning_system_prompt,
            formatting_instructions=build_review_formatting_instructions(input_data.profile),
            parse=_parse_chunk_review_response,
            tools=[],
            reasoning_think=True if think is None else think,
            system_prompt_content=system_prompt_content,
            agent_key="code_review",
            on_reasoning_agent=_capture,
            on_formatting=_capture_formatting,
            on_formatting_start=_on_formatting_start,
        )
    finally:
        now = time.monotonic()
        record_reasoning_transcript_turns(
            "chunk_review",
            target,
            turns=reasoning_turns,
            agent=reasoning_agent,
            fallback_prompt=prompt,
            started=started,
            reasoning_done_at=reasoning_done_at,
            system_prompt=reasoning_system_prompt,
            model=model_name,
            recorder=record_transcript_entry,
        )
        record_formatting_transcript_turns(
            "chunk_review",
            target,
            turns=format_turns,
            last_ended=now,
            system_prompt=formatting_system_prompt,
            model=model_name,
            recorder=record_transcript_entry,
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
