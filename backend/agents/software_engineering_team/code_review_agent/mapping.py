"""Map phase for the code-review coordinator: review each chunk, with caching.

Owns the per-chunk review call and everything around it: failure classification
(infrastructure vs recoverable content vs unexpected defect), retry/bisection
recovery, the degraded "not reviewed" fallback, the process-global map-phase
outcome cache (keyed on the chunk's exact LLM input + a context/model
fingerprint + the sibling surface), single-flight de-duplication of concurrent
identical chunks, the cross-file sibling-surface extraction, and the parallel
fan-out ``_map_chunks``.

It is also the home of the code-review fingerprint helpers — ``_stable_json_digest``
and ``_review_model_fingerprint`` plus the ``_context_fingerprint`` (map-phase) and
``_submission_fingerprint`` (coordinator submission-level short-circuit) keys built
on them — so the hashing primitive stays internal to the one module that owns the
cache-key machinery; the coordinator imports the fingerprints it needs.

Safety contract (see ``coordinator`` module docstring for the whole pipeline).
This is the canonical statement of the degrade/cache rationale — per-function
docstrings below state only their own contract and cross-reference this:
- Infrastructure failures raise ``CodeReviewUnavailableError`` immediately.
- Known content failures bisect/retry (and, for reasoning-only exhaustion or
  truncation, get one last-resort thinking-off retry), then degrade to a "not
  reviewed" coverage finding (``_degraded_outcome``) rather than aborting the
  run. By default that range is surfaced non-blockingly
  (``CodeReviewOutput.not_reviewed_ranges``) — never posted, never blocking;
  under ``CODE_REVIEW_BLOCK_ON_UNREVIEWED`` it becomes a blocking ``high``
  finding instead. The finding text names only the failure *class*
  (``type(exc).__name__``), never ``str(exc)``: parse/schema errors embed raw
  model output, and under the opt-out this text is published verbatim. Degraded
  findings live in ``_ChunkOutcome.not_reviewed_issues``, never ``issues``, so
  the false-positive filter — which only re-checks ``issues`` — can never drop
  a coverage/safety finding.
- Unexpected defects propagate unchanged (fail closed).
- Only an outcome from the exact full-chunk LLM input is cached (see
  ``_cached_review_chunk`` for exactly which outcomes qualify and why); a cache
  hit reproduces identical findings/verdicts (deep clone on store and retrieve),
  and a miss simply recomputes, so correctness never depends on a hit.
- Concurrent reviews of the *same* chunk key are de-duplicated to a single real
  review: one worker (the leader) runs it while the rest (waiters) block and
  reuse its outcome — or re-raise its exception — so byte-identical chunks that
  the parallel map fans out at the same time never fire redundant LLM calls,
  even before the first result is cached.

Known limitations:
- The sibling surface is a names-only, cross-file-only, best-effort heuristic, so
  a same-file cross-chunk rename or a sibling whose signature/contract changed but
  whose symbol name did not do not shift a cached chunk's key. This is a limit of
  the per-chunk map scope, not the cache: an uncached re-review is equally blind
  (a chunk's prompt never carries a sibling chunk's body, only names from other
  files), and the whole-submission false-positive pass — which always re-runs and
  can only remove findings — keeps the fail-safe intact.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from llm_service import (
    LLMClient,
    LLMJsonParseError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMSchemaValidationError,
    LLMSemanticExhaustionError,
    LLMTruncatedError,
    LLMUnreachableAfterRetriesError,
)
from shared.concurrency import parallel_map
from shared.env import env_flag_enabled
from shared.env_config import env_bool
from software_engineering_team.shared.context_sizing import (
    compute_code_review_sibling_surface_chars,
    parse_env_int,
)

from .chunk_reviewer import ChunkReviewAgent
from .chunking import (
    _bisect_chunk,
    _chunk_ranges,
    _issues_from_chunk_output,
    _map_parallelism,
    _max_bisect_depth,
    _segment_line_range,
    _segment_notes,
    _segment_range_label,
)
from .model_resolution import resolve_code_review_model, thinking_override_supported
from .models import (
    ChunkReviewInput,
    ChunkReviewOutput,
    CodeReviewInput,
    CodeReviewIssue,
    CodeReviewUnavailableError,
    ReviewChunk,
    ReviewProgressCallback,
    notify_review_progress,
)
from .side_effect_consolidation import SIDE_EFFECT_CONSOLIDATION_ENV

logger = logging.getLogger(__name__)

# Process-global map-phase outcome cache (see module docstring). Bounded LRU
# keyed on a content+context+model hash; guarded by a lock because the map phase
# fans chunks out across worker threads. ``0`` disables it (pure passthrough).
DEFAULT_CHUNK_OUTCOME_CACHE_SIZE = 512  # CODE_REVIEW_CHUNK_OUTCOME_CACHE_SIZE, floor 0

_CHUNK_OUTCOME_CACHE: "OrderedDict[str, _ChunkOutcome]" = OrderedDict()
_CHUNK_OUTCOME_CACHE_LOCK = threading.Lock()

# In-flight reviews, keyed by the same chunk cache key. A miss registers a
# pending ``Future`` here so concurrent workers asking for the identical chunk
# become waiters on the leader's ``future.result()`` rather than each firing the
# LLM (single-flight). The leader always resolves the future (result or
# exception) and releases the slot on every exit path. Guarded by
# ``_CHUNK_OUTCOME_CACHE_LOCK`` — every access is a bare dict get/set/del, never
# the review itself, so hold times stay tiny.
_CHUNK_INFLIGHT: "Dict[str, Future]" = {}


def _release_inflight(key: str, fut: "Future") -> None:
    """Drop this leader's in-flight slot, but only if it still holds ``fut``.

    Postconditions:
        - Removes ``key`` from ``_CHUNK_INFLIGHT`` iff its value is still this
          leader's future. The identity guard means a mid-flight cache clear that
          replaced the slot can never make one leader delete another's live
          registration. Idempotent: a no-op if the slot was already released.
    """
    with _CHUNK_OUTCOME_CACHE_LOCK:
        if _CHUNK_INFLIGHT.get(key) is fut:
            del _CHUNK_INFLIGHT[key]


def _chunk_outcome_cache_size() -> int:
    return parse_env_int(
        "CODE_REVIEW_CHUNK_OUTCOME_CACHE_SIZE", DEFAULT_CHUNK_OUTCOME_CACHE_SIZE, 0
    )


def clear_chunk_outcome_cache() -> None:
    """Drop every cached map-phase outcome and any in-flight registration.

    Postconditions:
        - The process-global cache is empty when this function returns. The next
          review of a chunk is a guaranteed miss only when no review of that
          chunk is currently in flight; a leader already in flight when this is
          called holds no lock across its LLM call (see ``_CHUNK_INFLIGHT``
          above) and can still write its outcome to the cache after this
          returns. Callers that must force a cold review should ensure no
          review is in flight (or await in-flight completion) before relying on
          a miss. Intended for tests (the cache persists across
          ``run_coordinator`` calls by design) and for callers that must force a
          cold review.
        - The in-flight registry is cleared too. In production it is empty
          whenever no review is running (a leader always pops its own slot); this
          keeps a test that clears mid-flight from stranding a stale record.
    """
    with _CHUNK_OUTCOME_CACHE_LOCK:
        _CHUNK_OUTCOME_CACHE.clear()
        # A leader already in flight (see _CHUNK_INFLIGHT) holds no lock across
        # its LLM call, so it can still write its outcome to the cache below
        # after this clear returns — see the postcondition above.
        _CHUNK_INFLIGHT.clear()


@dataclass
class _ChunkOutcome:
    """Accumulated result of reviewing one chunk (possibly via bisection).

    Invariants (see module docstring's safety contract for the full rationale):
        - ``approved_flags`` holds one entry per successful LLM sub-review; a
          degraded outcome contributes no entry — the range is never silently
          scored.
        - ``issues`` holds only genuine reviewer findings; degraded "not
          reviewed" coverage findings live in ``not_reviewed_issues`` instead.
    """

    issues: List[CodeReviewIssue] = field(default_factory=list)
    not_reviewed_issues: List[CodeReviewIssue] = field(default_factory=list)
    summaries: List[str] = field(default_factory=list)
    spec_notes: List[str] = field(default_factory=list)
    approved_flags: List[bool] = field(default_factory=list)
    # True when this outcome came from a reduced-fidelity thinking-off retry (see
    # ``_cached_review_chunk``'s ``cacheable`` check for why this and a bisected
    # recovery are excluded from the cache).
    degraded_recovery: bool = False

    def absorb(self, other: "_ChunkOutcome") -> None:
        """Append ``other``'s entries in order. Postcondition: no entry is lost."""
        self.issues.extend(other.issues)
        self.not_reviewed_issues.extend(other.not_reviewed_issues)
        self.summaries.extend(other.summaries)
        self.spec_notes.extend(other.spec_notes)
        self.approved_flags.extend(other.approved_flags)
        self.degraded_recovery = self.degraded_recovery or other.degraded_recovery

    def clone(self) -> "_ChunkOutcome":
        """Return a deep, independent copy.

        Postconditions:
            - The returned outcome shares no mutable state with ``self``: the
              lists are fresh and every ``CodeReviewIssue`` is deep-copied. This
              is what makes the map-phase cache safe — a cached entry is stored
              and served as a clone, so downstream mutation (dedupe, line
              re-anchoring, false-positive filtering, ``absorb``) can never
              corrupt it and every hit reproduces identical findings/verdicts.
        """
        return _ChunkOutcome(
            issues=[i.model_copy(deep=True) for i in self.issues],
            not_reviewed_issues=[i.model_copy(deep=True) for i in self.not_reviewed_issues],
            summaries=list(self.summaries),
            spec_notes=list(self.spec_notes),
            approved_flags=list(self.approved_flags),
            degraded_recovery=self.degraded_recovery,
        )


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield ``exc`` and its ``__cause__``/``__context__`` ancestors.

    The LLM client or caller wrappers may wrap the originating LLM error, so a
    chunk failure must be classified by walking the chain, not just its top type. Prefers
    an explicit ``__cause__`` (``raise ... from``) over the implicit ``__context__``,
    dedups by ``id()``, and stops after 10 hops.

    Postconditions:
        - Yields at most 10 distinct exceptions, starting with ``exc``; never
          raises. The single owner of this walk so the classifiers below cannot
          drift apart.
    """
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen and len(seen) < 10:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _is_infra_failure(exc: BaseException) -> bool:
    """Classify a chunk-review failure as infrastructure vs content-related.

    Infrastructure failures (rate limit, unreachable endpoint, auth/config
    errors) cannot be fixed by reviewing a smaller chunk, so retrying or
    bisecting them only multiplies doomed LLM calls. Content-related failures
    (JSON parse, schema validation, semantic exhaustion, anything else) may
    succeed on a smaller or repeated input.

    Postconditions:
        - Walks the exception chain via ``_exception_chain``; never raises.
    """
    for current in _exception_chain(exc):
        if isinstance(current, (LLMJsonParseError, LLMSchemaValidationError)):
            return False
        if isinstance(
            current,
            (LLMRateLimitError, LLMUnreachableAfterRetriesError, LLMPermanentError),
        ):
            return True
    return False


# Failures that represent the *model* (not our code) returning unusable output
# for a chunk. Only these may be retried/bisected and, if still unreviewable,
# degraded to a not-reviewed finding. Any other exception is treated as an
# unexpected defect and fails closed. ``json.JSONDecodeError`` is retained
# defensively: no current call path raises it directly anymore (the chunk
# reviewer now routes through ``llm_service.complete_validated``, which raises
# ``LLMJsonParseError`` or ``LLMSchemaValidationError`` instead), but any future
# bare ``json.loads`` reachable from this call chain would still classify
# correctly here. ``LLMTruncatedError``
# (finish_reason=length) is recoverable for the same reason bisection exists:
# a smaller chunk yields a smaller review, so a half that no longer exhausts the
# output-token budget parses cleanly; a chunk that still truncates at the
# bisection floor degrades to a blocking "not reviewed" finding instead of
# aborting the entire review job (the whole PR would otherwise fail on one
# oversized chunk).
_CONTENT_FAILURE_TYPES = (
    LLMJsonParseError,
    LLMSchemaValidationError,
    LLMSemanticExhaustionError,
    LLMTruncatedError,
    json.JSONDecodeError,
)


def _is_content_failure(exc: BaseException) -> bool:
    """Classify a chunk-review failure as a known, recoverable LLM content error.

    Postconditions:
        - Returns True only when the chain contains one of
          ``_CONTENT_FAILURE_TYPES`` (see that tuple's comment for why each
          member is included) — the failures a smaller or repeated input might
          fix, or that a human can be asked to review manually.
        - Returns False for everything else (e.g. ``KeyError``/``TypeError`` from
          a bug in the reviewer code), so unexpected defects fail closed instead
          of being masked as a not-reviewed finding.
        - Walks the exception chain via ``_exception_chain``; never raises.
    """
    return any(isinstance(c, _CONTENT_FAILURE_TYPES) for c in _exception_chain(exc))


def _semantic_exhaustion_in_chain(exc: BaseException) -> "Optional[LLMSemanticExhaustionError]":
    """Return the chain's ``LLMSemanticExhaustionError``, or None.

    The receipt object is returned (not just a bool) so callers can read its
    ``finish_reason`` and ``retry_thinking_level``: a ``finish_reason="length"``
    empty turn is a token-budget/truncation scenario where a smaller chunk can
    leave room for content — it must still line-split, like ``LLMTruncatedError`` —
    whereas a non-length reasoning-only exhaustion is input-size invariant (each
    half re-exhausts), and ``retry_thinking_level is None`` marks a no-ladder
    stochastic empty that a same-input retry may still recover.

    Postconditions:
        - Returns the first ``LLMSemanticExhaustionError`` found via
          ``_exception_chain``, else None. Never raises.
    """
    for current in _exception_chain(exc):
        if isinstance(current, LLMSemanticExhaustionError):
            return current
    return None


def _thinking_off_retry_enabled() -> bool:
    """Whether the last-resort thinking-off retry is enabled (default: on).

    Postconditions:
        - Returns ``False`` only for an explicit falsy ``CODE_REVIEW_THINKING_OFF_RETRY``
          (``false``/``0``/``no``/``off``); unset or anything else is ``True``.
          Kill switch for the retry that turns a reasoning-only/truncated chunk
          into a real review instead of a degraded not-reviewed range.
    """
    return env_bool("CODE_REVIEW_THINKING_OFF_RETRY", default=True)


def _chain_has(exc: BaseException, types: Tuple[type, ...]) -> bool:
    """Whether ``exc`` or its cause/context chain contains one of ``types``.

    Preconditions:
        - ``types`` may be empty; an empty tuple means no type can match.

    Postconditions:
        - Walks the ``__cause__``/``__context__`` chain via ``_exception_chain``
          and returns True on the first match. Empty ``types`` returns False.
          Never raises. Mirrors the traversal in ``_is_content_failure`` for a
          narrower type check (used to gate the thinking-off retry to the failures
          it can actually fix).
    """
    if not types:
        return False
    return any(isinstance(c, types) for c in _exception_chain(exc))


def _outcome_from_output(chunk: ReviewChunk, output: ChunkReviewOutput) -> _ChunkOutcome:
    """Build a successful chunk outcome from a completed reviewer output.

    Postconditions:
        - Returns one sub-review's worth of results (exactly one ``approved_flags``
          entry). A rejection with no extractable issues but a non-empty summary
          contributes one synthesized ``high`` issue built from that summary —
          applied per sub-review because at the merged level other chunks'
          findings would mask the empty-issues condition and the minor-only
          auto-approve net would silently discard the rejection. For a real
          LLM-produced ``output``, ``ChunkReviewLLMResponse``'s own consistency
          validator now rejects that exact shape (``approved=False`` with no
          actionable issue) at the schema layer before it ever reaches this
          function, so this synthesis is a defensive fallback for a
          directly-constructed ``ChunkReviewOutput`` rather than a normal part
          of the LLM call path.
    """
    issues = _issues_from_chunk_output(chunk, output.issues)
    if not output.approved and not issues and output.summary and output.summary.strip():
        issues = [
            CodeReviewIssue(
                severity="high",
                category="general",
                file_path="",
                description=f"Code review rejected: {output.summary}",
                suggestion="Address the concerns described in the review summary. "
                "Ensure the code meets all acceptance criteria and follows project conventions.",
            )
        ]
    return _ChunkOutcome(
        issues=issues,
        summaries=[output.summary],
        spec_notes=[output.spec_compliance_notes],
        approved_flags=[output.approved],
    )


def _degraded_outcome(chunk: ReviewChunk, exc: BaseException) -> _ChunkOutcome:
    """Build a degraded outcome for a chunk that survived recovery unreviewed.

    See the module docstring's safety contract for why this exists and how
    ``CODE_REVIEW_BLOCK_ON_UNREVIEWED`` changes its handling.

    Preconditions:
        - The failure was already classified a known content failure
          (``_is_content_failure``) — not infra, not an unexpected defect. Either
          it could be neither bisected further nor recovered by retry, OR it is a
          ladder-spent semantic exhaustion whose fast-path deliberately skips
          line-splitting and the same-input retry (both futile for it).

    Postconditions:
        - Returns one ``high``/``general`` finding per segment in the outcome's
          ``not_reviewed_issues`` (never ``issues``). Each finding spans the
          segment's original-file range via the model's multi-line convention
          (``start_line`` = first line, ``line`` = last line — there is no
          ``end_line`` field) and names the range in its description, so no
          covered line is silently dropped and downstream tools can highlight
          the full extent. The chunk casts no LLM approve/reject vote
          (``approved_flags`` is empty).
        - No ``summaries`` entry is produced, so the "not reviewed" condition
          never leaks into the merged review summary/PR body and never forces an
          extra synthesis LLM call on a partial run.
    """
    # Class name only, never str(exc) — see module docstring's safety contract.
    reason = type(exc).__name__
    issues = []
    for seg in chunk.segments:
        start, end = _segment_line_range(seg)
        issues.append(
            CodeReviewIssue(
                severity="high",
                category="general",
                file_path=seg.path,
                start_line=start,
                line=end,
                description=(
                    f"This code could not be reviewed automatically ({reason}); "
                    f"{_segment_range_label(seg)} was not reviewed. Blocking review "
                    "so unreviewed code is not approved."
                ),
                suggestion="Review this section manually; the automated reviewer could not process it.",
            )
        )
    # See the "No summaries entry" postcondition above.
    return _ChunkOutcome(not_reviewed_issues=issues)


def _bisect_halves_run_sequentially(llm: LLMClient) -> bool:
    """True when a bisected chunk's two halves must be reviewed one at a time.

    Mirrors ``coordinator._tail_passes_run_sequentially``: scripted
    ``DummyLLMClient`` doubles use a shared non-thread-safe response index, so
    fanning the halves out concurrently would corrupt scripted call order/count.
    Duplicated locally rather than imported — ``coordinator`` imports from this
    module, so the reverse import would be circular.

    Postconditions: returns ``True`` iff ``llm`` is (or wraps) a
    ``DummyLLMClient``. Pure.
    """
    from llm_service.clients.dummy import DummyLLMClient

    if isinstance(llm, DummyLLMClient):
        return True
    return isinstance(getattr(llm, "client", None), DummyLLMClient)


def _run_reviewer_call(
    reviewer: ChunkReviewAgent,
    chunk_input: ChunkReviewInput,
    run_limiter: Optional[threading.Semaphore],
    **kwargs: Any,
) -> ChunkReviewOutput:
    """Invoke ``reviewer.run``, honoring the run-wide concurrency ceiling.

    The single choke point every actual chunk-review LLM call passes through —
    the top-level review, a same-input retry, the thinking-off retry, and each
    bisection half all route through here — so a semaphore threaded down from
    ``run_coordinator`` can cap the *total* number of concurrent
    ``reviewer.run`` calls for one review run, not just the outer map-phase
    fan-out width (see ``_map_chunks``'s ``run_limiter`` parameter).

    Preconditions:
        - ``run_limiter`` is ``None`` (no run-wide ceiling — a direct caller,
          a test, or the Temporal per-activity call path, none of which share
          a limiter object across chunk reviews) or a ``threading.Semaphore``
          sized to the run's ``_map_parallelism()`` budget.

    Postconditions:
        - Returns ``reviewer.run(chunk_input, **kwargs)``'s result or lets its
          exception propagate unchanged.
        - When ``run_limiter`` is given, the permit is acquired immediately
          before the call and released immediately after (success or
          exception) — held only for this one call's duration, never across a
          caller's recursion — so a chunk that goes on to bisect after this
          call returns never blocks its own children on a permit it is still
          holding, and the semaphore's capacity always equals the true
          run-wide ceiling on in-flight ``reviewer.run`` calls.
    """
    if run_limiter is None:
        return reviewer.run(chunk_input, **kwargs)
    run_limiter.acquire()
    try:
        return reviewer.run(chunk_input, **kwargs)
    finally:
        run_limiter.release()


def _review_chunk_with_recovery(
    reviewer: ChunkReviewAgent,
    chunk: ReviewChunk,
    base_input: Dict,
    sibling_surface: str = "",
    surface_by_path: Optional[Dict[str, List[str]]] = None,
    depth: int = 0,
    retried: bool = False,
    run_limiter: Optional[threading.Semaphore] = None,
) -> _ChunkOutcome:
    """Review one chunk, recovering from content failures by retry or bisection.

    Preconditions:
        - ``base_input`` holds the shared ``ChunkReviewInput`` fields
          (task/spec/architecture context), not per-chunk fields.
        - ``sibling_surface`` is *this* chunk's view of the other changed files'
          top-level symbols (used for its prompt and, at the top level, its cache
          key). ``surface_by_path`` is the whole submission's surface map; when a
          multi-file chunk bisects, each half **recomputes** its own sibling
          surface from it (the half no longer contains the other half's files, so
          those files are now genuine siblings and their surface must be shown).
          A same-input retry keeps the surface unchanged.
        - ``run_limiter`` is ``None`` or a ``threading.Semaphore`` sized to
          ``_map_parallelism()`` for this run (see ``_run_reviewer_call``).
          Threaded unchanged through every recursive call this function makes
          (same-input retry, each bisection half) so the ceiling it enforces
          covers the whole recovery tree, not just this call.

    Postconditions:
        - Returns an outcome covering every line of the chunk — every line is
          either reviewed or recorded as a "not reviewed" range — or raises. The
          chunk is never silently skipped or scored.
        - Infrastructure failures raise ``CodeReviewUnavailableError``
          immediately, without retry or bisection.
        - Unexpected failures (anything not classified by ``_is_content_failure``
          — e.g. a ``KeyError``/``TypeError`` from a reviewer bug) propagate
          unchanged: they fail closed so the defect surfaces, rather than being
          masked as a not-reviewed finding.
        - Known content failures bisect up to the depth cap; the two halves are
          reviewed concurrently (via ``parallel_map``) unless ``reviewer.llm`` is
          a scripted ``DummyLLMClient`` double or ``_map_parallelism() <= 1``, in
          which case they run sequentially exactly as before — see
          ``_bisect_halves_run_sequentially``. Either way, results merge via
          ``_ChunkOutcome.absorb()`` in a fixed halves[0]-then-halves[1] order,
          independent of which half's call actually finishes first. Any chunk
          that cannot bisect further — the original or a bisected child — gets
          one same-input retry, except a ladder-spent reasoning-loop exhaustion,
          which skips both (see the bisect-vs-retry decision comment in the
          recovery section below for the reasoning-loop vs. truncation
          distinction). A reasoning-only or truncated terminal failure gets one
          further thinking-off retry (env-gated, production path only). A
          failure that survives all of that degrades via ``_degraded_outcome``
          rather than aborting the whole run (see module docstring's safety
          contract). A one-off transient error in a terminal child therefore
          never costs even that child's review.
        - Recovery (bisection / retry) is dispatched OUTSIDE the ``except`` block
          so a child failure is never implicitly context-chained to this chunk's
          exception (which ``_semantic_exhaustion_in_chain`` would otherwise
          misread).
        - A sub-review that rejects with no extractable issues but a non-empty
          summary contributes one synthesized high issue built from that
          summary (see ``_outcome_from_output``): applied per sub-review, because
          at the merged level other chunks' findings would mask the empty-issues
          condition and the minor-only auto-approve net would silently discard
          the rejection.
        - Every actual ``reviewer.run`` call this function (or a recursive call
          of it) makes goes through ``_run_reviewer_call``, so when
          ``run_limiter`` is given, the total number of concurrent
          ``reviewer.run`` calls across the top-level review, any same-input
          retry, the thinking-off retry, and both bisection halves never
          exceeds the semaphore's capacity — a true run-wide ceiling, not just
          a cap on the two-worker bisection pool below.
    """
    chunk_input = ChunkReviewInput(
        code_chunk=chunk.content,
        file_path_or_label=chunk.paths_label,
        segment_note=_segment_notes(chunk),
        sibling_surface=sibling_surface,
        **base_input,
    )
    failure: Optional[BaseException] = None
    try:
        output = _run_reviewer_call(reviewer, chunk_input, run_limiter)
    except Exception as exc:
        if _is_infra_failure(exc):
            raise CodeReviewUnavailableError(
                f"Review model unavailable ({type(exc).__name__}: {exc}); "
                "no verdict was produced for this submission.",
                unreviewed=_chunk_ranges(chunk),
            ) from exc
        if not _is_content_failure(exc):
            # Not a known LLM content error — likely a defect in the reviewer
            # code (KeyError/TypeError, a malformed return shape, etc.). Fail
            # closed so the bug surfaces, rather than masking it as a
            # not-reviewed finding that another approving chunk could carry
            # past the gate.
            raise
        # Known content failure: stash it and recover BELOW, outside this `except`
        # block (see the recovery comment) — never recurse/retry in here.
        failure = exc
    else:
        # A clean review: build the outcome (including the empty-issues rejection
        # synthesis) via the shared ``_outcome_from_output`` helper — the same one
        # the thinking-off retry uses — so the synthesized-issue format lives in a
        # single place.
        return _outcome_from_output(chunk, output)

    # --- Recovery for a known content failure -------------------------------
    # This runs AFTER the `except` block has exited, deliberately: the child
    # ``reviewer.run`` calls below must NOT execute while ``failure`` is the active
    # exception, or Python would implicitly chain it onto any child failure's
    # ``__context__`` — and ``_semantic_exhaustion_in_chain`` walks ``__context__``,
    # so a child truncation/parse error would be misclassified as semantic and
    # stripped of its own line-bisect/retry recovery.
    exc = failure
    # A REASONING-LOOP semantic exhaustion (``finish_reason != "length"``: the model
    # emitted only reasoning and stopped) is content-shaped but NOT input-size-shaped.
    # LINE-splitting a single file only multiplies doomed multi-minute calls (each
    # half re-exhausts), and a same-input retry re-runs the downgrade ladder the
    # client already spent — so a ladder-spent reasoning-loop exhaustion skips both.
    # A ``finish_reason="length"`` empty turn, by contrast, is token-budget-bound (the
    # model ran out of tokens mid-reasoning): a smaller chunk can leave room for
    # content, so it line-splits like ``LLMTruncatedError`` (it is NOT a reasoning
    # loop here). SEPARATING a multi-file chunk is worthwhile either way (only one
    # file may be the culprit), so a multi-segment reasoning-loop chunk is split by
    # file; and a reasoning-loop exhaustion where NO ladder ran (thinking was already
    # off) is a stochastic empty that keeps its one same-input retry. Other content
    # failures (JSON parse, length truncation) line-bisect as before.
    sem_exc = _semantic_exhaustion_in_chain(exc)
    reasoning_loop = sem_exc is not None and sem_exc.finish_reason != "length"
    skip_retry = reasoning_loop and sem_exc.retry_thinking_level is not None
    can_bisect = depth < _max_bisect_depth() and (not reasoning_loop or len(chunk.segments) > 1)
    halves = _bisect_chunk(chunk) if can_bisect else None
    if halves is not None:
        logger.warning(
            "CodeReviewCoordinator: chunk review failed at depth %s (%s: %s) — bisecting [%s]",
            depth,
            type(exc).__name__,
            exc,
            chunk.paths_label,
        )
        # Each half recomputes its sibling surface: a half no longer contains
        # the other half's files, so those files become genuine siblings whose
        # surface it should see (when surface_by_path is unavailable — a direct
        # caller passed None — the parent's surface rides along unchanged).
        branches: List[Callable[[], _ChunkOutcome]] = [
            lambda half=halves[0]: _review_chunk_with_recovery(
                reviewer,
                half,
                base_input,
                _half_sibling_surface(half, surface_by_path, sibling_surface),
                surface_by_path,
                depth + 1,
                run_limiter=run_limiter,
            ),
            lambda half=halves[1]: _review_chunk_with_recovery(
                reviewer,
                half,
                base_input,
                _half_sibling_surface(half, surface_by_path, sibling_surface),
                surface_by_path,
                depth + 1,
                run_limiter=run_limiter,
            ),
        ]
        # Each half is a fully independent recursive call (no shared mutable
        # state), so they fan out concurrently — unless the LLM double can't
        # tolerate it or the operator has capped parallelism to 1. Either way,
        # ``results[0]``/``results[1]`` preserve halves[0]/halves[1] order (see
        # ``parallel_map``'s ``preserve_order`` default), so the merge below is
        # identical to the sequential path regardless of completion order.
        if _bisect_halves_run_sequentially(reviewer.llm) or _map_parallelism() <= 1:
            outcome = branches[0]()
            outcome.absorb(branches[1]())
        else:
            results = parallel_map(branches, lambda fn: fn(), max_workers=2, skip_none=False)
            outcome = results[0]
            outcome.absorb(results[1])
        return outcome
    # A same-input retry is worthwhile unless it is futile: a ladder-spent
    # reasoning-loop exhaustion (``skip_retry``) would only re-run the model's
    # already spent thinking ladder, and a chunk that already retried gets no
    # second one.
    if not retried and not skip_retry:
        logger.warning(
            "CodeReviewCoordinator: chunk review failed (%s: %s) — retrying once [%s]",
            type(exc).__name__,
            exc,
            chunk.paths_label,
        )
        return _review_chunk_with_recovery(
            reviewer,
            chunk,
            base_input,
            sibling_surface,
            surface_by_path,
            depth,
            retried=True,
            run_limiter=run_limiter,
        )
    # Last resort before degrading: for the content failures a non-thinking pass can
    # fix — a reasoning-only response (``LLMSemanticExhaustionError``) or an
    # output-token truncation (``LLMTruncatedError``) — retry once with thinking
    # forced OFF. This turns the common "the model thought but never answered" case
    # (including a ladder-spent reasoning loop) into a real review instead of a
    # not-reviewed range, which is what makes the degraded finding rare. Gated by env
    # and by ``thinking_override_supported(reviewer.llm)`` (an injected test
    # ``LLMClient`` that doesn't support a thinking-level override, so tests skip
    # this and keep their call counts). Any failure here falls through to the
    # degrade below.
    if (
        _thinking_off_retry_enabled()
        and thinking_override_supported(reviewer.llm)
        and _chain_has(exc, (LLMSemanticExhaustionError, LLMTruncatedError))
    ):
        logger.warning(
            "CodeReview: last-resort thinking-off retry after %s [%s]",
            type(exc).__name__,
            chunk.paths_label,
        )
        try:
            recovered = _run_reviewer_call(reviewer, chunk_input, run_limiter, think=False)
        except Exception as exc2:
            # This best-effort retry runs after the original content failure has been
            # handled; an infra failure still surfaces as unavailable, anything else
            # means the retry did not help, so fall through to degrade on the original
            # failure — no worse than not having attempted it. A genuine reviewer-code
            # bug is not masked: it would have failed closed on the first attempt,
            # before this last-resort retry.
            if _is_infra_failure(exc2):
                raise CodeReviewUnavailableError(
                    f"Review model unavailable ({type(exc2).__name__}: {exc2}); "
                    "no verdict was produced for this submission.",
                    unreviewed=_chunk_ranges(chunk),
                ) from exc2
        else:
            outcome = _outcome_from_output(chunk, recovered)
            # Reduced-fidelity recovery: don't freeze it under the full-chunk cache
            # key (see ``_ChunkOutcome.degraded_recovery``).
            outcome.degraded_recovery = True
            return outcome
    # Terminal content failure: degrade instead of aborting the whole run (see
    # module docstring's safety contract). Chunks that did succeed still
    # contribute their verdicts — a run where *no* chunk succeeds is caught by
    # ``run_coordinator``'s total-failure guard. Stable, greppable telemetry so
    # operators can count how often the reviewer gives up on a chunk (the
    # condition the user wants to be rare); ``exc`` is logged here, never
    # published — the degraded finding names only the class.
    logger.warning(
        "CodeReview degrade: failure_class=%s ranges=%s detail=%s",
        type(exc).__name__,
        _chunk_ranges(chunk),
        exc,
    )
    return _degraded_outcome(chunk, exc)


def _review_model_fingerprint(llm: LLMClient) -> str:
    """Best-effort stable identifier for the model chunk reviews will run on.

    Preconditions:
        - ``llm`` is the client that will be handed to ``ChunkReviewAgent``.

    Postconditions:
        - Returns a string that changes when the resolved review model changes,
          so it can invalidate the map-phase cache (a cached outcome from one
          model is never served for another). Never raises: any failure to
          resolve the model falls back to the client's type name. The value is
          identity-only — it is hashed into the cache key, never published.
    """
    try:
        model = resolve_code_review_model(llm)
    except Exception:
        # Best-effort: never let a fingerprinting failure abort a review. Log it
        # so an unexpected model-resolution failure (import/config mistake) is
        # visible to operators rather than silently degrading cache keys.
        logger.warning(
            "CodeReviewCoordinator: model fingerprint resolution failed; "
            "falling back to client type name",
            exc_info=True,
        )
        return type(llm).__name__
    for attr in ("model_id", "model_name", "model"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    config = getattr(model, "config", None)
    if isinstance(config, dict):
        candidate = config.get("model_id") or config.get("model")
        if isinstance(candidate, str) and candidate:
            return candidate
    return type(model).__name__


# Top-level symbol declarations whose rename/removal in one file can break a
# reference in another: Python ``def``/``class`` and TS/JS ``export`` bindings
# (named or ``export { ... }`` lists). Extraction is heuristic and only feeds
# reviewer *context* — over- or under-matching never gates the review, so a
# tolerant regex is fine. The Python pattern is anchored at column zero (no
# leading whitespace) so only *module-level* defs/classes count — an indented
# method or nested function is not a cross-file-referenceable top-level symbol,
# and advertising one could mask a removed module-level name of the same spelling.
_PY_SYMBOL_RE = re.compile(r"^(?:async[ \t]+)?(?:def|class)[ \t]+([A-Za-z_]\w*)", re.MULTILINE)
_TS_EXPORT_RE = re.compile(
    r"^[ \t]*export[ \t]+(?:default[ \t]+)?(?:async[ \t]+)?"
    r"(?:function|class|const|let|var|interface|type|enum)[ \t]+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
_TS_EXPORT_LIST_RE = re.compile(r"^[ \t]*export[ \t]*\{([^}]*)\}", re.MULTILINE)


def _symbol_surface(content: str) -> List[str]:
    """Extract a file's top-level defined/exported symbol names.

    Postconditions:
        - Returns a sorted, de-duplicated list of Python ``def``/``class`` names
          and TS/JS ``export`` binding names (including names inside
          ``export { a, b as c }``, where the exported name ``c`` is taken).
          Heuristic and best-effort — used only as reviewer context, never to
          gate a verdict.
    """
    names: set[str] = set()
    for match in _PY_SYMBOL_RE.finditer(content):
        names.add(match.group(1))
    for match in _TS_EXPORT_RE.finditer(content):
        names.add(match.group(1))
    for match in _TS_EXPORT_LIST_RE.finditer(content):
        for item in match.group(1).split(","):
            token = item.strip()
            if not token:
                continue
            # ``a as b`` exports the alias ``b``; a bare name exports itself.
            exported = token.split()[-1]
            if re.fullmatch(r"[A-Za-z_$][\w$]*", exported):
                names.add(exported)
    return sorted(names)


def _surface_by_path(blocks: List[Tuple[str, str]]) -> Dict[str, List[str]]:
    """Map each named block path to its top-level symbol surface.

    Postconditions:
        - One entry per block with a non-empty path *and* a non-empty surface;
          headerless ('') blocks and symbol-less files are omitted, so the map
          holds only files whose public surface can be referenced cross-file.
    """
    surface: Dict[str, List[str]] = {}
    for path, content in blocks:
        if not path:
            continue
        names = _symbol_surface(content)
        if names:
            surface[path] = names
    return surface


def _sibling_surface(chunk: ReviewChunk, surface_by_path: Dict[str, List[str]]) -> str:
    """Render the top-level surface of the changed files *outside* this chunk.

    Preconditions:
        - ``surface_by_path`` is the whole submission's ``_surface_by_path``.

    Postconditions:
        - Returns a deterministic, path-sorted ``"path: name1, name2"``-per-line
          string covering changed files outside this chunk, bounded by the
          configured sibling-surface prompt budget. A zero budget disables the
          block. Because it is derived only from sibling files, editing a file's
          *body* without changing its top-level symbols leaves this string (and
          any cache key built from it) unchanged.
    """
    own_paths = {seg.path for seg in chunk.segments if seg.path}
    lines = [
        f"{path}: {', '.join(surface_by_path[path])}"
        for path in sorted(surface_by_path)
        if path not in own_paths
    ]
    return "\n".join(lines)[: compute_code_review_sibling_surface_chars()]


def _half_sibling_surface(
    half: ReviewChunk,
    surface_by_path: Optional[Dict[str, List[str]]],
    fallback: str,
) -> str:
    """Sibling surface for a bisected half.

    Postconditions:
        - Recomputes ``_sibling_surface(half, surface_by_path)`` when the map is
          available, so a half sees the surface of the sibling files that used to
          share its parent chunk. Falls back to the parent's ``fallback`` surface
          when ``surface_by_path`` is None (a direct caller that did not thread
          the map through), preserving the previous ride-along behavior.
    """
    if surface_by_path is None:
        return fallback
    return _sibling_surface(half, surface_by_path)


def _stable_json_digest(payload: Dict) -> str:
    """SHA-256 of a JSON-native mapping, deterministic across runs.

    Preconditions:
        - Every value in ``payload`` (recursively) is natively JSON-serializable
          (str/number/bool/list/dict/None); a non-serializable value raises
          ``TypeError`` rather than being coerced, so a caller bug surfaces
          instead of silently producing an unstable key.

    Postconditions:
        - Returns a hex digest that is identical for two payloads with the same
          contents regardless of key insertion order (``sort_keys=True``), and
          differs whenever any value differs. The single hashing idiom shared by
          the map-phase context fingerprint and the submission fingerprint.
    """
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _context_fingerprint(base_input: Dict, model_fingerprint: str) -> str:
    """Hash the review inputs shared by every chunk in one coordinator run.

    Preconditions:
        - ``base_input`` is the shared ``ChunkReviewInput`` field dict built in
          ``run_coordinator``. Every value must be natively JSON-serializable
          (str/number/bool/list/dict/None) or an object exposing a ``.value``
          attribute whose value is JSON-serializable — every value (not just
          ``profile``) is normalized via ``getattr(value, "value", value)``
          before hashing; ``profile`` is typically a ``ReviewProfile`` handled
          the same way as any other enum-like field.

    Postconditions:
        - Returns a hex digest that changes whenever any shared review input
          (task/spec/architecture/acceptance/user-decisions/language/profile) or
          the resolved model changes, so the map-phase cache invalidates on a
          changed profile, task context, or model. Deterministic and stable
          across runs (``sort_keys`` + enum ``.value`` normalization), so a hit
          for an unchanged chunk survives across coordinator calls in a process.
        - Raises ``TypeError`` if a future change puts a non-serializable value
          in ``base_input``: the key is failed loud rather than coerced via
          ``str()`` (which could be non-deterministic and silently break the
          cache) — a precondition violation surfaces instead of hiding.
    """
    profile = base_input.get("profile")
    normalized = {
        key: (getattr(value, "value", value))
        for key, value in base_input.items()
        if key != "profile"
    }
    normalized["profile"] = getattr(profile, "value", profile)
    normalized["__model__"] = model_fingerprint
    return _stable_json_digest(normalized)


def _submission_fingerprint(input_data: CodeReviewInput, model_fingerprint: str) -> str:
    """Hash the whole raw submission plus the resolved model.

    The submission-level analogue of ``_context_fingerprint`` (which keys only the
    shared context); it keys the *entire* input so the coordinator's
    submission-level short-circuit can recognise a byte-identical resubmission.

    Preconditions:
        - ``input_data`` is a valid ``CodeReviewInput``.
        - ``model_fingerprint`` is ``_review_model_fingerprint(llm)`` for the
          client that would run the review.

    Postconditions:
        - Returns a hex digest that changes whenever **any** input field (or the
          resolved model) changes, and also whenever the output-affecting
          ``CODE_REVIEW_SIDE_EFFECT_CONSOLIDATION`` toggle flips. It is derived
          from ``input_data.model_dump()`` plus that toggle, so it keys on the
          whole input (not a hand-picked subset) plus consolidation identity: a
          new ``CodeReviewInput`` field is hashed automatically and can never be
          silently dropped. Two submissions collide only when their full inputs
          and consolidation setting are identical, so a hit means the review
          would be the same work. (Every current field is verdict-affecting, so
          this is exactly the submission identity; a future non-verdict field
          would only cause extra misses — full re-reviews — never a stale hit.)
        - Computed from raw fields only (no compaction/LLM), so the short-circuit
          it guards fires before any model call. Deterministic (``sort_keys``),
          so a stored approval survives across coordinator calls in a process.
    """
    payload = input_data.model_dump(mode="json")
    payload["__model__"] = model_fingerprint
    payload["__side_effect_consolidation__"] = env_flag_enabled(SIDE_EFFECT_CONSOLIDATION_ENV)
    return _stable_json_digest(payload)


def _chunk_cache_key(chunk: ReviewChunk, context_fp: str, sibling_surface: str) -> str:
    """Key one chunk's map-phase review by its exact LLM input plus context.

    Postconditions:
        - Combines the chunk's rendered content, segment notes, and the
          sibling-surface context (the bytes the reviewer actually sees) with the
          run's context fingerprint, so two chunks collide only when their LLM
          inputs are byte-identical. Including ``sibling_surface`` invalidates a
          cached chunk when a *sibling* changed file's public surface changed
          (e.g. a renamed/removed export), so the reviewer re-runs with that new
          surface — a body-only sibling edit leaves the surface (and the key)
          unchanged, preserving the hit.
    """
    body = f"{context_fp}\x00{chunk.content}\x00{_segment_notes(chunk)}\x00{sibling_surface}"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _cached_review_chunk(
    reviewer: ChunkReviewAgent,
    chunk: ReviewChunk,
    base_input: Dict,
    context_fp: str,
    sibling_surface: str = "",
    surface_by_path: Optional[Dict[str, List[str]]] = None,
    run_limiter: Optional[threading.Semaphore] = None,
) -> _ChunkOutcome:
    """Review one chunk, reusing a cached or in-flight map-phase outcome.

    Preconditions:
        - Same as ``_review_chunk_with_recovery`` for ``base_input`` and
          ``run_limiter``.
        - ``context_fp`` is the run's ``_context_fingerprint`` (folds in the
          shared context and the resolved model).
        - ``sibling_surface`` is this chunk's view of the other changed files'
          top-level symbols (see ``_sibling_surface``); it is fed to the reviewer
          and folded into the cache key. ``surface_by_path`` is the whole
          submission's surface map, threaded to recovery so a bisected half
          recomputes its own sibling surface.

    Postconditions:
        - When caching is disabled (non-positive
          ``CODE_REVIEW_CHUNK_OUTCOME_CACHE_SIZE``) this is a pure passthrough to
          ``_review_chunk_with_recovery`` — no caching and no single-flight,
          identical to no cache at all.
        - On a hit, returns a deep clone of the stored outcome (never the shared
          instance), so the caller may mutate it freely; findings/verdicts are
          reproduced identically.
        - Single-flight: at most one worker (the leader) runs the real review for
          a given chunk key at a time. Concurrent workers asking for the same key
          block until the leader finishes, then reuse a deep clone of its outcome
          (or re-raise its exception) instead of firing a duplicate LLM call — so
          byte-identical chunks the parallel map fans out simultaneously trigger a
          single review, even before the result is cached. This handoff covers
          *every* outcome, including the degraded/bisected ones that are never
          stored in the LRU.
        - On a leader miss, runs the real review and, only when the outcome is
          the exact full-chunk result, stores a clone under the chunk key,
          evicting the oldest entry past capacity — see the ``cacheable`` check
          below for the precise criteria and why each excluded case is excluded.
        - Never suppresses ``_review_chunk_with_recovery``'s exceptions
          (infrastructure failure, unexpected defect): the leader re-raises them
          unchanged and hands the same exception to its waiters, so they fail the
          same way rather than re-running. The failed key is left uncached and
          its in-flight slot cleared, so the next cycle retries for real.
        - ``run_limiter`` is passed through unchanged to the leader's
          ``_review_chunk_with_recovery`` call (never consulted by a cache
          hit or a waiter, since neither fires an LLM call of its own).
    """
    capacity = _chunk_outcome_cache_size()
    if capacity <= 0:
        return _review_chunk_with_recovery(
            reviewer, chunk, base_input, sibling_surface, surface_by_path, run_limiter=run_limiter
        )

    key = _chunk_cache_key(chunk, context_fp, sibling_surface)
    with _CHUNK_OUTCOME_CACHE_LOCK:
        hit = _CHUNK_OUTCOME_CACHE.get(key)
        if hit is not None:
            _CHUNK_OUTCOME_CACHE.move_to_end(key)
        else:
            fut = _CHUNK_INFLIGHT.get(key)
            is_leader = fut is None
            if is_leader:
                # No cached result and no review under way: register a pending
                # future so concurrent duplicates wait on us instead of reviewing.
                fut = _CHUNK_INFLIGHT[key] = Future()
    if hit is not None:
        # Clone outside the lock: a stored entry is never mutated in place, so the
        # captured reference stays valid even if another thread evicts it, and the
        # deep copy no longer serializes other workers on the cache lock.
        return hit.clone()

    if not is_leader:
        # Waiter: an identical chunk is already being reviewed. Block on the
        # leader's future rather than firing a second LLM call — ``result()``
        # returns the outcome (or re-raises the leader's exception) with the
        # right happens-before, and returns at once if the leader already
        # finished. Clone per caller so each owns an isolated copy.
        return fut.result().clone()

    # Leader: run the single real review for this key, then resolve the future.
    # Everything that can raise (the review and the outcome clones) is inside the
    # ``try`` so the future is ALWAYS resolved and the slot ALWAYS released — a
    # leader that failed to resolve it would hang every waiter on the untimed
    # ``result()`` above and poison the key for the life of the process.
    try:
        outcome = _review_chunk_with_recovery(
            reviewer, chunk, base_input, sibling_surface, surface_by_path, run_limiter=run_limiter
        )
        # Cache only an outcome produced from the *exact full-chunk* LLM input: no
        # degraded ("not reviewed") coverage findings, and exactly one sub-review.
        # A degraded outcome must be retried for real next cycle. Requiring a
        # single sub-review also excludes a bisected recovery (the full chunk
        # raised a recoverable content error and only succeeded after splitting):
        # its aggregate has >= 2 approved_flags and reflects lower-context,
        # split-across-the-boundary reviews. Freezing that under the full-chunk key
        # would keep serving the reduced-fidelity result on later identical cycles
        # instead of retrying the full chunk — so we skip it and let the next cycle
        # re-attempt full context.
        cacheable = (
            not outcome.not_reviewed_issues
            and not outcome.degraded_recovery
            and len(outcome.approved_flags) == 1
        )
        # Clone before taking the lock so the deep copy doesn't serialize other
        # workers; ``outcome`` is a local not yet shared, so this is race-free.
        stored = outcome.clone() if cacheable else None
        # Store (if cacheable) and release the slot under a single lock acquisition.
        with _CHUNK_OUTCOME_CACHE_LOCK:
            if stored is not None:
                _CHUNK_OUTCOME_CACHE[key] = stored
                _CHUNK_OUTCOME_CACHE.move_to_end(key)
                while len(_CHUNK_OUTCOME_CACHE) > capacity:
                    _CHUNK_OUTCOME_CACHE.popitem(last=False)
            if _CHUNK_INFLIGHT.get(key) is fut:
                del _CHUNK_INFLIGHT[key]
        # An isolated copy for any waiters (they clone again per caller), so a
        # waiter can never observe the caller mutating the leader's outcome.
        published = outcome.clone()
    except BaseException as exc:
        # Fail closed for the caller and every waiter: hand them the same
        # exception (so they don't re-run rather than re-raise), free the slot
        # (only if still ours — a mid-flight cache clear may have replaced it) so a
        # later cycle can retry for real, and re-raise unchanged.
        _release_inflight(key, fut)
        fut.set_exception(exc)
        raise

    # Nothing above the try's end can strand a waiter: the slot is already
    # released and ``set_result`` cannot raise for a pending future.
    fut.set_result(published)
    return outcome


# Progress-bar band this phase reports into: [_MAP_PHASE_START, _MAP_PHASE_START +
# _MAP_PHASE_SPAN]. ``_MAP_PHASE_START`` must stay equal to coordinator.py's
# ``_PROGRESS_CHUNKING_DONE`` (the checkpoint reported just before this phase starts) --
# the two live in different modules and are not otherwise coupled in code.
_MAP_PHASE_START = 0.10
_MAP_PHASE_SPAN = 0.80


def _map_chunks(
    chunk_reviewer: ChunkReviewAgent,
    chunks: List[ReviewChunk],
    base_input: Dict,
    context_fp: str,
    surface_by_path: Dict[str, List[str]],
    progress_callback: Optional[ReviewProgressCallback] = None,
    run_limiter: Optional[threading.Semaphore] = None,
) -> List[_ChunkOutcome]:
    """Review all chunks, fanning out independent map calls.

    Preconditions:
        - ``chunk_reviewer`` is safe for concurrent ``run`` calls: the agent is
          stateless and ``_run_chunk_review`` invokes the injected ``LLMClient``
          directly, so the only object shared across workers is that injected
          LLM client, whose central implementations guard their own state
          (clients injected here must support concurrent calls).
        - ``context_fp`` is the run's ``_context_fingerprint``; each chunk is
          reviewed through ``_cached_review_chunk``, which reuses a prior
          map-phase outcome when the chunk's LLM input and this fingerprint are
          both unchanged (a miss simply recomputes, so results are identical).
        - ``surface_by_path`` is the whole submission's ``_surface_by_path``;
          each chunk's ``_sibling_surface`` (the other changed files' top-level
          symbols) is fed to its reviewer and folded into its cache key.
        - ``run_limiter`` is ``None`` (no run-wide ceiling — e.g. a direct
          caller or a test) or a ``threading.Semaphore`` sized to this run's
          ``_map_parallelism()``, created once by the caller (``run_coordinator``)
          and shared across every chunk in the run — never one created per call
          to this function, or concurrently-bisecting chunks would each get
          their own independent budget instead of sharing one.

    Postconditions:
        - Returns one outcome per chunk in input order. A content failure that
          survives recovery yields a degraded outcome rather than raising, so
          it does not abort the fan-out. Only an infrastructure failure raises
          ``CodeReviewUnavailableError``; the first such failure is observed as
          it happens — never delayed behind an earlier, slower chunk — pending
          chunks are cancelled, and the exception propagates immediately;
          already-running reviews are left to finish in the background rather
          than blocking the failure behind in-flight model calls.
        - When ``progress_callback`` is provided, one ``reviewing`` report is
          emitted per completed chunk ("chunk i/N reviewed", i = completion
          order) with fractions in (0.10, 0.90]; the counter update and the
          callback run under one lock, so fractions stay non-decreasing even
          with parallel workers.
        - After a failure propagates, no further progress is ever reported:
          abandoned in-flight workers finish in the background with their
          callback suppressed, so stale "reviewing" reports can never
          overwrite the caller's failure state.
        - ``run_limiter``, when given, is passed unchanged to every chunk's
          ``_cached_review_chunk`` call, so it gates every actual
          ``reviewer.run`` call this fan-out (and any bisection recovery it
          triggers) makes — not just the outer worker-pool width computed
          below, which only bounds how many chunks are dequeued at once.
    """
    total = len(chunks)
    progress_lock = threading.Lock()
    completed_count = [0]
    abandoned = threading.Event()

    def _run_one(chunk: ReviewChunk) -> _ChunkOutcome:
        sibling_surface = _sibling_surface(chunk, surface_by_path)
        outcome = _cached_review_chunk(
            chunk_reviewer,
            chunk,
            base_input,
            context_fp,
            sibling_surface,
            surface_by_path,
            run_limiter=run_limiter,
        )
        with progress_lock:
            if not abandoned.is_set():
                completed_count[0] += 1
                notify_review_progress(
                    progress_callback,
                    "reviewing",
                    f"chunk {completed_count[0]}/{total} reviewed: {chunk.paths_label}",
                    _MAP_PHASE_START + _MAP_PHASE_SPAN * completed_count[0] / total,
                )
        return outcome

    def _abandon() -> None:
        # Setting the flag under the progress lock guarantees any in-flight
        # report finishes before the failure propagates and none follows it.
        with progress_lock:
            abandoned.set()

    workers = min(_map_parallelism(), total)
    if workers <= 1:
        # Sequential in the caller's thread (CODE_REVIEW_MAP_PARALLELISM=1, the
        # documented "run calls sequentially" mode): a failure aborts immediately
        # and a later chunk is never started — a 1-worker pool could otherwise
        # dequeue and begin the next chunk's review before the main thread
        # observes the failure and cancels, firing an extra LLM call past fail-fast.
        # Context is already the caller's here, so attribution still propagates.
        return [_run_one(c) for c in chunks]

    # parallel_map owns the bounded pool, input-order results, fast-fail on the
    # first exception (pending chunks cancelled, original traceback preserved),
    # and per-task context propagation so LLM attribution reaches the workers.
    # Outcomes are never None, so skip_none is off; _abandon runs before any
    # cancellation so abandoned in-flight workers suppress their progress.
    return parallel_map(
        chunks,
        _run_one,
        max_workers=workers,
        skip_none=False,
        on_first_exception=_abandon,
    )
