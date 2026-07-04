"""Map phase for the code-review coordinator: review each chunk, with caching.

Owns the per-chunk review call and everything around it: failure classification
(infrastructure vs recoverable content vs unexpected defect), retry/bisection
recovery, the degraded "not reviewed" fallback, the process-global map-phase
outcome cache (keyed on the chunk's exact LLM input + a context/model
fingerprint + the sibling surface), single-flight de-duplication of concurrent
identical chunks, the cross-file sibling-surface extraction, and the parallel
fan-out ``_map_chunks``.

Safety contract (see ``coordinator`` module docstring for the whole pipeline):
- Infrastructure failures raise ``CodeReviewUnavailableError`` immediately.
- Known content failures bisect/retry, then degrade to a blocking ``high`` "not
  reviewed" finding rather than aborting the run.
- Unexpected defects propagate unchanged (fail closed).
- Only fully-reviewed outcomes are cached; degraded outcomes never are. A cache
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
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

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
from shared_concurrency import parallel_map
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
from .model_resolution import resolve_code_review_model
from .models import (
    ChunkReviewInput,
    CodeReviewIssue,
    CodeReviewUnavailableError,
    ReviewChunk,
    ReviewProgressCallback,
    notify_review_progress,
)

logger = logging.getLogger(__name__)

# Process-global map-phase outcome cache (see module docstring). Bounded LRU
# keyed on a content+context+model hash; guarded by a lock because the map phase
# fans chunks out across worker threads. ``0`` disables it (pure passthrough).
DEFAULT_CHUNK_OUTCOME_CACHE_SIZE = 512  # CODE_REVIEW_CHUNK_OUTCOME_CACHE_SIZE, floor 0

_CHUNK_OUTCOME_CACHE: "OrderedDict[str, _ChunkOutcome]" = OrderedDict()
_CHUNK_OUTCOME_CACHE_LOCK = threading.Lock()

# In-flight reviews, keyed by the same chunk cache key. A miss registers one
# ``_InflightReview`` here so concurrent workers asking for the identical chunk
# become waiters on the leader rather than each firing the LLM (single-flight).
# Guarded by ``_CHUNK_OUTCOME_CACHE_LOCK`` — every access is a bare dict
# get/set/pop, never the review itself, so hold times stay tiny.
_CHUNK_INFLIGHT: "Dict[str, _InflightReview]" = {}


class _InflightReview:
    """One in-progress map-phase review that concurrent workers can wait on.

    The first worker to miss the cache for a chunk key becomes the *leader*: it
    runs the single real review and, when done, publishes the result here and
    wakes any *waiters* — later workers that found this record and blocked
    instead of issuing their own duplicate LLM call.

    Invariants:
        - The leader ALWAYS fires ``done`` exactly once, on every exit path
          (success or exception), having first assigned ``error`` — or, when at
          least one waiter is registered, ``outcome``. ``threading.Event``
          provides the happens-before edge, so a waiter that returns from
          ``done.wait()`` reads the published value safely.
        - ``waiters`` counts the workers that joined this record while the leader
          held the ``_CHUNK_INFLIGHT`` slot (incremented under the cache lock).
          The leader snapshots it under the same lock when releasing the slot, so
          a non-zero count means a waiter will read ``outcome`` and the leader
          must publish an isolated clone; a zero count lets the solo-leader path
          skip that clone. Workers arriving after the slot is released never see
          this record — they start a fresh review — so they are never counted.
    """

    __slots__ = ("done", "outcome", "error", "waiters")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.outcome: Optional["_ChunkOutcome"] = None
        self.error: Optional[BaseException] = None
        self.waiters = 0


def _chunk_outcome_cache_size() -> int:
    return parse_env_int(
        "CODE_REVIEW_CHUNK_OUTCOME_CACHE_SIZE", DEFAULT_CHUNK_OUTCOME_CACHE_SIZE, 0
    )


def clear_chunk_outcome_cache() -> None:
    """Drop every cached map-phase outcome and any in-flight registration.

    Postconditions:
        - The process-global cache is empty; the next review of any chunk is a
          guaranteed miss. Intended for tests (the cache persists across
          ``run_coordinator`` calls by design) and for callers that must force a
          cold review.
        - The in-flight registry is cleared too. In production it is empty
          whenever no review is running (a leader always pops its own slot); this
          keeps a test that clears mid-flight from stranding a stale record.
    """
    with _CHUNK_OUTCOME_CACHE_LOCK:
        _CHUNK_OUTCOME_CACHE.clear()
        _CHUNK_INFLIGHT.clear()


@dataclass
class _ChunkOutcome:
    """Accumulated result of reviewing one chunk (possibly via bisection).

    Invariants:
        - ``approved_flags`` holds one entry per successful LLM sub-review. A
          chunk that could not be reviewed (a known content failure surviving
          recovery) contributes a degraded outcome — a blocking ``high`` "not
          reviewed" finding and no ``approved_flags`` entry — rather than
          aborting the run, so unreviewed code is rejected, not silently scored.
        - ``issues`` holds only genuine reviewer findings; the degraded "not
          reviewed" coverage findings live in ``not_reviewed_issues``. Keeping
          them apart lets the false-positive filter re-check the genuine
          findings without ever being able to drop a coverage/safety finding.
    """

    issues: List[CodeReviewIssue] = field(default_factory=list)
    not_reviewed_issues: List[CodeReviewIssue] = field(default_factory=list)
    summaries: List[str] = field(default_factory=list)
    spec_notes: List[str] = field(default_factory=list)
    commit_messages: List[str] = field(default_factory=list)
    approved_flags: List[bool] = field(default_factory=list)

    def absorb(self, other: "_ChunkOutcome") -> None:
        """Append ``other``'s entries in order. Postcondition: no entry is lost."""
        self.issues.extend(other.issues)
        self.not_reviewed_issues.extend(other.not_reviewed_issues)
        self.summaries.extend(other.summaries)
        self.spec_notes.extend(other.spec_notes)
        self.commit_messages.extend(other.commit_messages)
        self.approved_flags.extend(other.approved_flags)

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
            commit_messages=list(self.commit_messages),
            approved_flags=list(self.approved_flags),
        )


def _is_infra_failure(exc: BaseException) -> bool:
    """Classify a chunk-review failure as infrastructure vs content-related.

    Infrastructure failures (rate limit, unreachable endpoint, auth/config
    errors) cannot be fixed by reviewing a smaller chunk, so retrying or
    bisecting them only multiplies doomed LLM calls. Content-related failures
    (JSON parse, schema validation, semantic exhaustion, anything else) may
    succeed on a smaller or repeated input.

    Postconditions:
        - Walks the ``__cause__``/``__context__`` chain (strands may wrap the
          client error) up to a bounded depth; never raises.
    """
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen and len(seen) < 10:
        seen.add(id(current))
        if isinstance(current, (LLMJsonParseError, LLMSchemaValidationError)):
            return False
        if isinstance(
            current,
            (LLMRateLimitError, LLMUnreachableAfterRetriesError, LLMPermanentError),
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


# Failures that represent the *model* (not our code) returning unusable output
# for a chunk. Only these may be retried/bisected and, if still unreviewable,
# degraded to a not-reviewed finding. Any other exception is treated as an
# unexpected defect and fails closed. ``json.JSONDecodeError`` is included
# because the chunk reviewer parses the model's reply with a bare
# ``json.loads`` — malformed model JSON surfaces as that raw error, not an
# ``LLMJsonParseError``, and is just as recoverable. ``LLMTruncatedError``
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
        - Returns True only when the chain contains a known model-content
          failure (``LLMJsonParseError``, ``LLMSchemaValidationError``,
          ``LLMSemanticExhaustionError``, ``LLMTruncatedError`` — a
          finish_reason=length token-limit truncation — or a raw
          ``json.JSONDecodeError`` from parsing the model's reply) — the failures
          a smaller or repeated input might fix, or that a human can be asked to
          review manually.
        - Returns False for everything else (e.g. ``KeyError``/``TypeError`` from
          a bug in the reviewer code), so unexpected defects fail closed instead
          of being masked as a not-reviewed finding.
        - Walks the ``__cause__``/``__context__`` chain (strands may wrap the
          client error) up to a bounded depth; never raises.
    """
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen and len(seen) < 10:
        seen.add(id(current))
        if isinstance(current, _CONTENT_FAILURE_TYPES):
            return True
        current = current.__cause__ or current.__context__
    return False


def _degraded_outcome(chunk: ReviewChunk, exc: BaseException) -> _ChunkOutcome:
    """Build a degraded outcome for a chunk that survived recovery unreviewed.

    A known LLM content failure that survives retry and bisection down to an
    un-splittable chunk does not abort the whole run: the chunk's code is named
    by a blocking "not reviewed" finding so the gate rejects the review and a
    human is alerted, while sibling chunks that succeeded still contribute their
    own verdicts.

    Preconditions:
        - The failure was already classified a known content failure
          (``_is_content_failure``) — not infra, not an unexpected defect — and
          could be neither bisected further nor recovered by retry.

    Postconditions:
        - Returns one ``high``/``general`` finding per segment in the outcome's
          ``not_reviewed_issues`` (never ``issues``), so the false-positive
          filter — which only re-checks genuine ``issues`` — can never drop a
          coverage finding. Each finding spans the segment's original-file range
          via the model's multi-line convention (``start_line`` = first line,
          ``line`` = last line — there is no ``end_line`` field) and names the
          range in its description, so no covered line is silently dropped and
          downstream tools can highlight the full extent.
        - The findings are ``high`` severity, so ``_reconcile_approval`` rejects
          the merged review: unreviewed code can never pass the code-review gate
          as approved (the backend only feeds issues back on rejection). The
          chunk casts no LLM approve/reject vote (``approved_flags`` is empty);
          the block comes from the finding's severity.
        - The finding text names only the failure *class*, never ``str(exc)``,
          so raw model output carried by parse/schema errors is never published
          downstream (e.g. by the ``/review-pr`` flow).
    """
    # Name only the failure *class*, never ``str(exc)``: parse/schema errors
    # embed raw model output (e.g. ``LLMJsonParseError`` carries a 500-char
    # response preview), and this finding is published verbatim by the
    # ``/review-pr`` flow — interpolating the message would leak arbitrary
    # model output / code excerpts into PR comments.
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
    return _ChunkOutcome(
        not_reviewed_issues=issues,
        summaries=[f"Not reviewed: {', '.join(_chunk_ranges(chunk))} ({reason})."],
    )


def _review_chunk_with_recovery(
    reviewer: ChunkReviewAgent,
    chunk: ReviewChunk,
    base_input: Dict,
    sibling_surface: str = "",
    surface_by_path: Optional[Dict[str, List[str]]] = None,
    depth: int = 0,
    retried: bool = False,
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

    Postconditions:
        - Returns an outcome covering every line of the chunk — every line is
          either reviewed or named by a blocking ``high`` "not reviewed"
          finding — or raises. The chunk is never silently skipped or scored.
        - Infrastructure failures raise ``CodeReviewUnavailableError``
          immediately, without retry or bisection.
        - Unexpected failures (anything not classified by ``_is_content_failure``
          — e.g. a ``KeyError``/``TypeError`` from a reviewer bug) propagate
          unchanged: they fail closed so the defect surfaces, rather than being
          masked as a not-reviewed finding.
        - Known content failures bisect up to the depth cap; any chunk that
          cannot bisect further — the original or a bisected child — gets
          exactly one same-input retry. A terminal content failure that survives
          the retry degrades to a blocking ``high`` not-reviewed finding (via
          ``_degraded_outcome``) rather than aborting the whole run; a one-off
          transient error in a terminal child therefore never costs even that
          child's review.
        - A sub-review that rejects with no extractable issues but a non-empty
          summary contributes one synthesized high issue built from that
          summary: applied here, per sub-review, because at the merged level
          other chunks' findings would mask the empty-issues condition and the
          minor-only auto-approve net would silently discard the rejection.
    """
    chunk_input = ChunkReviewInput(
        code_chunk=chunk.content,
        file_path_or_label=chunk.paths_label,
        segment_note=_segment_notes(chunk),
        sibling_surface=sibling_surface,
        **base_input,
    )
    try:
        output = reviewer.run(chunk_input)
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
        halves = _bisect_chunk(chunk) if depth < _max_bisect_depth() else None
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
            outcome = _review_chunk_with_recovery(
                reviewer,
                halves[0],
                base_input,
                _half_sibling_surface(halves[0], surface_by_path, sibling_surface),
                surface_by_path,
                depth + 1,
            )
            outcome.absorb(
                _review_chunk_with_recovery(
                    reviewer,
                    halves[1],
                    base_input,
                    _half_sibling_surface(halves[1], surface_by_path, sibling_surface),
                    surface_by_path,
                    depth + 1,
                )
            )
            return outcome
        if not retried:
            logger.warning(
                "CodeReviewCoordinator: chunk review failed (%s: %s) — retrying once [%s]",
                type(exc).__name__,
                exc,
                chunk.paths_label,
            )
            return _review_chunk_with_recovery(
                reviewer, chunk, base_input, sibling_surface, surface_by_path, depth, retried=True
            )
        # Known content failure that cannot bisect further and survived its
        # retry: degrade instead of aborting the whole run. The chunk's code is
        # named by a blocking ``high`` "not reviewed" finding (which rejects the
        # merged review, so unreviewed code is never approved), while the chunks
        # that did succeed still contribute their verdicts. (A run in which *no*
        # chunk succeeds is caught by ``run_coordinator``'s total-failure guard,
        # which still raises.)
        logger.warning(
            "CodeReviewCoordinator: chunk unreviewable after recovery (%s: %s) — "
            "degrading to a not-reviewed finding [%s]",
            type(exc).__name__,
            exc,
            chunk.paths_label,
        )
        return _degraded_outcome(chunk, exc)
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
        commit_messages=[output.suggested_commit_message],
        approved_flags=[output.approved],
    )


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
        logger.debug(
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

# Cap per file so one huge file can't dominate a chunk's sibling-surface context.
_MAX_SYMBOLS_PER_FILE = 60


def _symbol_surface(content: str) -> List[str]:
    """Extract a file's top-level defined/exported symbol names.

    Postconditions:
        - Returns a sorted, de-duplicated list of Python ``def``/``class`` names
          and TS/JS ``export`` binding names (including names inside
          ``export { a, b as c }``, where the exported name ``c`` is taken).
          Capped at ``_MAX_SYMBOLS_PER_FILE``. Heuristic and best-effort — used
          only as reviewer context, never to gate a verdict.
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
    return sorted(names)[:_MAX_SYMBOLS_PER_FILE]


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
          string covering every changed file whose path is not one of this
          chunk's own paths, truncated to ``CODE_REVIEW_SIBLING_SURFACE_CHARS``.
          Empty when no sibling file has a surface. Because it is derived only
          from sibling files, editing a file's *body* without changing its
          top-level symbols leaves this string (and any cache key built from it)
          unchanged. Capping here (rather than only in the prompt builder) keeps
          the cache key hashing the exact bytes the reviewer sees, so an edit
          past the cap can't cause a spurious miss on an otherwise-identical
          prompt.
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


def _context_fingerprint(base_input: Dict, model_fingerprint: str) -> str:
    """Hash the review inputs shared by every chunk in one coordinator run.

    Preconditions:
        - ``base_input`` is the shared ``ChunkReviewInput`` field dict built in
          ``run_coordinator``. Every value must be natively JSON-serializable
          (str/number/bool/list/dict/None) except ``profile``, which is a
          ``ReviewProfile`` normalized to its ``.value`` here.

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
    payload = json.dumps(normalized, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
) -> _ChunkOutcome:
    """Review one chunk, reusing a cached or in-flight map-phase outcome.

    Preconditions:
        - Same as ``_review_chunk_with_recovery`` for ``base_input``.
        - ``context_fp`` is the run's ``_context_fingerprint`` (folds in the
          shared context and the resolved model).
        - ``sibling_surface`` is this chunk's view of the other changed files'
          top-level symbols (see ``_sibling_surface``); it is fed to the reviewer
          and folded into the cache key. ``surface_by_path`` is the whole
          submission's surface map, threaded to recovery so a bisected half
          recomputes its own sibling surface.

    Postconditions:
        - When caching is disabled (``CODE_REVIEW_CHUNK_OUTCOME_CACHE_SIZE`` ==
          0) this is a pure passthrough to ``_review_chunk_with_recovery`` — no
          caching and no single-flight, identical to no cache at all.
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
        - On a leader miss, runs the real review and — only when the outcome came
          from the exact full-chunk LLM input (no ``not_reviewed_issues`` and
          *exactly one* ``approved_flags`` entry) — stores a clone under the
          chunk key, evicting the oldest entry past capacity. Degraded outcomes
          (a transient failure is retried for real next cycle) and bisected
          recoveries (>= 2 sub-reviews, reduced context — re-attempted at full
          context next cycle) are never cached.
        - Never suppresses ``_review_chunk_with_recovery``'s exceptions
          (infrastructure failure, unexpected defect): the leader re-raises them
          unchanged and hands the same exception to its waiters, so they fail the
          same way rather than re-running. The failed key is left uncached and
          its in-flight slot cleared, so the next cycle retries for real.
    """
    capacity = _chunk_outcome_cache_size()
    if capacity <= 0:
        return _review_chunk_with_recovery(
            reviewer, chunk, base_input, sibling_surface, surface_by_path
        )

    key = _chunk_cache_key(chunk, context_fp, sibling_surface)
    with _CHUNK_OUTCOME_CACHE_LOCK:
        hit = _CHUNK_OUTCOME_CACHE.get(key)
        if hit is not None:
            _CHUNK_OUTCOME_CACHE.move_to_end(key)
        else:
            inflight = _CHUNK_INFLIGHT.get(key)
            if inflight is None:
                # No cached result and no review under way: take ownership so
                # concurrent duplicates wait on us instead of re-reviewing.
                inflight = _InflightReview()
                _CHUNK_INFLIGHT[key] = inflight
                is_leader = True
            else:
                # Join the in-flight review; the count is snapshotted by the
                # leader (under this same lock) to decide whether to publish.
                inflight.waiters += 1
                is_leader = False
    if hit is not None:
        # Clone outside the lock: a stored entry is never mutated in place, so the
        # captured reference stays valid even if another thread evicts it, and the
        # deep copy no longer serializes other workers on the cache lock.
        return hit.clone()

    if not is_leader:
        # Waiter: an identical chunk is already being reviewed. Block for it and
        # reuse its result rather than firing a second LLM call. ``inflight`` was
        # captured under the lock, so it stays valid even after the leader releases
        # the slot; ``Event.wait`` returns at once if the leader already finished.
        inflight.done.wait()
        if inflight.error is not None:
            raise inflight.error
        return inflight.outcome.clone()

    # Leader: run the single real review for this key, then publish to waiters.
    # Everything that can raise (the review and the outcome clones) is inside the
    # ``try`` so the record is ALWAYS resolved and the slot ALWAYS released — a
    # leader that failed to fire ``done`` would hang every waiter on the untimed
    # ``wait()`` above and poison the key for the life of the process.
    try:
        outcome = _review_chunk_with_recovery(
            reviewer, chunk, base_input, sibling_surface, surface_by_path
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
        if not outcome.not_reviewed_issues and len(outcome.approved_flags) == 1:
            # Clone before acquiring the lock so the deep copy doesn't serialize
            # other workers; ``outcome`` is a local not yet shared, so this is
            # race-free.
            stored = outcome.clone()
            with _CHUNK_OUTCOME_CACHE_LOCK:
                _CHUNK_OUTCOME_CACHE[key] = stored
                _CHUNK_OUTCOME_CACHE.move_to_end(key)
                while len(_CHUNK_OUTCOME_CACHE) > capacity:
                    _CHUNK_OUTCOME_CACHE.popitem(last=False)
        # Snapshot the waiter count and release the slot atomically: a waiter that
        # registered before this point is counted (it will read ``outcome``); one
        # arriving after finds no slot and starts its own review. Publish an
        # isolated clone only when someone is actually waiting — the common
        # solo-leader path skips that extra deep copy.
        with _CHUNK_OUTCOME_CACHE_LOCK:
            has_waiters = inflight.waiters > 0
            if _CHUNK_INFLIGHT.get(key) is inflight:
                del _CHUNK_INFLIGHT[key]
        inflight.outcome = outcome.clone() if has_waiters else outcome
    except BaseException as exc:
        # Fail closed for the caller and every waiter: hand them the same
        # exception (so they don't re-run), free the slot (only if it is still
        # ours — a mid-flight cache clear may have replaced it) so a later cycle
        # can retry for real, wake the waiters, and re-raise unchanged.
        with _CHUNK_OUTCOME_CACHE_LOCK:
            if _CHUNK_INFLIGHT.get(key) is inflight:
                del _CHUNK_INFLIGHT[key]
        inflight.error = exc
        inflight.done.set()
        raise

    # Resolved cleanly: nothing below can raise, so ``done`` always fires here.
    inflight.done.set()
    return outcome


def _map_chunks(
    chunk_reviewer: ChunkReviewAgent,
    chunks: List[ReviewChunk],
    base_input: Dict,
    context_fp: str,
    surface_by_path: Dict[str, List[str]],
    progress_callback: Optional[ReviewProgressCallback] = None,
) -> List[_ChunkOutcome]:
    """Review all chunks, fanning out independent map calls.

    Preconditions:
        - ``chunk_reviewer`` is safe for concurrent ``run`` calls: the agent is
          stateless and ``_run_chunk_review`` builds a fresh strands agent and
          model per call, so the only object shared across workers is the
          injected LLM client, whose central implementations guard their own
          state (clients injected here must support concurrent calls).
        - ``context_fp`` is the run's ``_context_fingerprint``; each chunk is
          reviewed through ``_cached_review_chunk``, which reuses a prior
          map-phase outcome when the chunk's LLM input and this fingerprint are
          both unchanged (a miss simply recomputes, so results are identical).
        - ``surface_by_path`` is the whole submission's ``_surface_by_path``;
          each chunk's ``_sibling_surface`` (the other changed files' top-level
          symbols) is fed to its reviewer and folded into its cache key.

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
    """
    total = len(chunks)
    progress_lock = threading.Lock()
    completed_count = [0]
    abandoned = threading.Event()

    def _run_one(chunk: ReviewChunk) -> _ChunkOutcome:
        sibling_surface = _sibling_surface(chunk, surface_by_path)
        outcome = _cached_review_chunk(
            chunk_reviewer, chunk, base_input, context_fp, sibling_surface, surface_by_path
        )
        with progress_lock:
            if not abandoned.is_set():
                completed_count[0] += 1
                notify_review_progress(
                    progress_callback,
                    "reviewing",
                    f"chunk {completed_count[0]}/{total} reviewed: {chunk.paths_label[:120]}",
                    0.10 + 0.80 * completed_count[0] / total,
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
