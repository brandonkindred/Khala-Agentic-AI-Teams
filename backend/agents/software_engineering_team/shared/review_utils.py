"""Shared documentation self-review orchestration for the V2 sub-teams.

The backend and frontend Code-V2 teams each run an iterative documentation
self-review pass: code context is rendered into bounded, function-aware chunks
and the evolving documentation is refined chunk-by-chunk across several
iterations. That chunking and iteration logic is language-agnostic and was
previously duplicated byte-for-byte in each team's ``phases/review.py``; this
module owns it so it lives in one place. Each team passes in its own
``DOCUMENTATION_SELF_REVIEW_PROMPT``, ``parse_documentation_self_review_template``,
and ``DocumentationSelfReviewResult`` factory, plus an ``invoke_model`` callable
(so the Strands ``Agent`` build stays the team module's patch surface).

The LLM-based code-review fallback's shared core lives in
``software_engineering_team.shared.v2_review``
(``run_coordinator_llm_review``); the tuning constants both that fallback and
this self-review use are owned here.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, TypeVar

logger = logging.getLogger(__name__)

# The two V2 teams each own a distinct ``DocumentationSelfReviewResult`` type, so
# the helper is generic over whatever the caller's ``result_factory`` produces.
ResultT = TypeVar("ResultT")

# ---------------------------------------------------------------------------
# Constants (shared by the LLM code-review fallback and the doc self-review)
# ---------------------------------------------------------------------------

MAX_REVIEW_CODE_CHARS = 60_000  # Generous limit; review all files, not just first 20
# A file this many chunks deep means an unusually large review; log a warning
# for cost/rate-limit visibility, but still review every chunk (dropping any
# would re-introduce the tail truncation this fallback exists to avoid).
MANY_CHUNKS_WARN_THRESHOLD = 20

MIN_DOC_SELF_REVIEW_ITERATIONS = 3
MAX_DOC_SELF_REVIEW_ITERATIONS = 3
DOC_QUALITY_THRESHOLD = 0.9

# Per-call code-context budget for the documentation self-review. Smaller than
# MAX_REVIEW_CODE_CHARS because the doc-review prompt also carries the full
# documentation being refined plus the template.
MAX_DOC_REVIEW_CHUNK_CHARS = 40_000


def doc_review_code_chunks(code_files: Dict[str, str]) -> List[str]:
    """Render the code context as bounded, function-aware chunks.

    Preconditions:
        - ``code_files`` maps file paths to their full source text.

    Postconditions:
        - Every non-blank file is covered exactly once across the returned
          strings, split only on function/method/class boundaries; no file is
          dropped and no file is clipped mid-content. Each string's length is
          bounded by ``MAX_DOC_REVIEW_CHUNK_CHARS`` (except a single over-budget
          segment placed alone, per ``build_review_chunks``' contract).
        - Returns ``["(No code context)"]`` when there is no non-blank code, so
          the review still runs one pass.
    """
    # Imported lazily, fully-qualified, to match this package's existing
    # convention for code_review_agent imports (see ``shared.v2_review``).
    from software_engineering_team.code_review_agent.coordinator import build_review_chunks

    blocks = [(p, c) for p, c in code_files.items() if c and c.strip()]
    if not blocks:
        return ["(No code context)"]
    chunks = list(build_review_chunks(blocks, MAX_DOC_REVIEW_CHUNK_CHARS))
    if len(chunks) > MANY_CHUNKS_WARN_THRESHOLD:
        logger.warning(
            "Documentation self-review: %d code chunk(s) for %d file(s) — large input",
            len(chunks),
            len(blocks),
        )
    return [chunk.content for chunk in chunks]


def run_documentation_self_review(
    *,
    documentation: Dict[str, str],
    code_files: Dict[str, str],
    prompt_template: str,
    parse_template: Callable[[str], Dict[str, Any]],
    result_factory: Callable[..., ResultT],
    invoke_model: Callable[[str], str],
    task_description: str = "",
    min_iterations: int = MIN_DOC_SELF_REVIEW_ITERATIONS,
    max_iterations: int = MAX_DOC_SELF_REVIEW_ITERATIONS,
    quality_threshold: float = DOC_QUALITY_THRESHOLD,
    detail_callback: Optional[Callable[[str], None]] = None,
) -> ResultT:
    """Self-review documentation across iterations for quality refinement.

    This function iteratively reviews and improves documentation files. It always
    runs at least ``min_iterations`` times, and continues up to ``max_iterations``
    unless the quality score exceeds the threshold. Unlike other review phases, it
    never "fails" — it always produces refined documentation.

    The team-specific pieces are injected: ``prompt_template`` (the team's
    ``DOCUMENTATION_SELF_REVIEW_PROMPT``), ``parse_template`` (the team's
    ``parse_documentation_self_review_template``), ``result_factory`` (the team's
    ``DocumentationSelfReviewResult``), and ``invoke_model`` (which runs one prompt
    through the team's LLM — built in the team module so the Strands ``Agent``
    stays patchable there).

    Preconditions:
        - ``documentation`` maps doc file paths to their current content;
          ``code_files`` maps code file paths to their full source text.
        - ``prompt_template`` accepts ``iteration``, ``max_iterations``,
          ``task_description``, ``documentation``, and ``code`` format fields.
        - ``parse_template`` returns a dict that may contain ``"quality_score"``
          (float), ``"improvements"`` (list), and ``"files"`` (path -> content).
        - ``result_factory`` accepts keyword arguments ``documentation``,
          ``iterations``, ``final_quality_score``, ``improvements_made``, and
          ``summary``.
        - ``invoke_model`` runs one prompt through the team's LLM and returns the
          raw text response.
        - ``max_iterations`` >= 1; ``0.0`` <= ``quality_threshold`` <= ``1.0``.

    Postconditions:
        - Code context is rendered once (it does not change across iterations) via
          ``doc_review_code_chunks``; one LLM call is made per code chunk per
          iteration, threading the evolving docs through so every code slice
          informs the refinement.
        - An iteration's score is the minimum across its chunks (conservative: a
          later slice exposing a gap must not let us stop early). A chunk whose
          LLM call or parse fails is logged and skipped, and any chunk failure in
          an iteration suppresses that iteration's early-stop so the next
          iteration re-reviews every chunk.
        - Always performs at least ``min_iterations`` (when no chunk fails) and at
          most ``max_iterations``; returns ``result_factory(...)`` carrying the
          refined documentation, iterations performed, final score, accumulated
          improvements, and a summary string.
    """
    current_docs = dict(documentation)
    all_improvements: List[str] = []
    final_score = 0.5
    iterations_performed = 0

    # Function-aware, bounded code context: every file is covered, none clipped
    # mid-function, and no prompt exceeds the per-call budget. Computed once —
    # the code being documented does not change across iterations.
    code_chunks = doc_review_code_chunks(code_files)

    for iteration in range(1, max_iterations + 1):
        iterations_performed = iteration

        if detail_callback:
            detail_callback(f"Documentation self-review iteration {iteration}/{max_iterations}...")

        logger.info(
            "Documentation self-review iteration %d/%d. Quality threshold: %.2f",
            iteration,
            max_iterations,
            quality_threshold,
        )

        # One LLM call per code chunk, threading the evolving docs through so
        # every chunk of code informs the refinement. The iteration's score is
        # the minimum across chunks (conservative: a later code slice exposing a
        # documentation gap must not let us stop early). If any chunk fails this
        # iteration, the early-stop gate is suppressed so the next iteration
        # re-reviews every chunk — a transient failure on one chunk must not let
        # high scores on the others end the review with that chunk's code unseen.
        iteration_score: Optional[float] = None
        iteration_improvements = 0
        iteration_updates = 0
        chunk_failures = 0
        # Render the evolving documentation once per iteration, then re-render
        # only when a chunk actually updates a file (below) so later chunks still
        # see earlier refinements. Documentation is passed in full (no clip) so
        # the model can rewrite any file's tail; callers are expected to keep
        # per-microtask documentation within the model's context budget. Rebuilding
        # this for every chunk when nothing changed was an O(chunks x docs) waste
        # for large doc sets.
        doc_text = "\n\n".join(f"--- {p} ---\n{c}" for p, c in current_docs.items())
        for chunk_idx, code_chunk in enumerate(code_chunks, start=1):
            prompt = prompt_template.format(
                iteration=iteration,
                max_iterations=max_iterations,
                task_description=task_description or "No specific task description",
                documentation=doc_text if doc_text else "(No documentation files yet)",
                code=code_chunk,
            )

            try:
                raw = invoke_model(prompt)
                parsed = parse_template(raw)
            except Exception as exc:
                # Covers both the LLM call and parsing: a malformed response must
                # not abort the review — log and move to the next chunk.
                logger.warning(
                    "Documentation self-review chunk failed (iteration %d, chunk %d/%d): %s",
                    iteration,
                    chunk_idx,
                    len(code_chunks),
                    exc,
                )
                chunk_failures += 1
                continue

            quality_score = parsed.get("quality_score", 0.5)
            improvements = parsed.get("improvements", [])
            updated_files = parsed.get("files", {})

            iteration_score = (
                quality_score if iteration_score is None else min(iteration_score, quality_score)
            )
            all_improvements.extend(improvements)
            iteration_improvements += len(improvements)

            if updated_files:
                current_docs.update(updated_files)
                iteration_updates += len(updated_files)
                # Docs changed; re-render so subsequent chunks see the refinement.
                doc_text = "\n\n".join(f"--- {p} ---\n{c}" for p, c in current_docs.items())

        if iteration_score is None:
            # Every chunk's LLM call failed this iteration; keep prior score.
            logger.info(
                "Documentation self-review iteration %d: all %d chunk(s) failed, score unchanged",
                iteration,
                len(code_chunks),
            )
            continue

        final_score = iteration_score
        logger.info(
            "Documentation self-review iteration %d: score=%.2f, updated %d file(s), %d improvements",
            iteration,
            final_score,
            iteration_updates,
            iteration_improvements,
        )

        if iteration >= min_iterations and final_score >= quality_threshold and chunk_failures == 0:
            logger.info(
                "Documentation self-review complete: reached quality threshold %.2f >= %.2f after %d iterations",
                final_score,
                quality_threshold,
                iteration,
            )
            break

    summary = (
        f"Documentation self-review completed after {iterations_performed} iteration(s). "
        f"Final quality score: {final_score:.2f}. "
        f"Total improvements made: {len(all_improvements)}."
    )
    logger.info(summary)

    if detail_callback:
        detail_callback(f"Documentation self-review complete (score: {final_score:.2f})")

    return result_factory(
        documentation=current_docs,
        iterations=iterations_performed,
        final_quality_score=final_score,
        improvements_made=all_improvements,
        summary=summary,
    )
