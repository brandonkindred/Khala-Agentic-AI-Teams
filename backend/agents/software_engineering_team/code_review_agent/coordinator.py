"""Code Review Coordinator: map-reduce review with bounded per-call prompts.

Pipeline: input → (path, content) blocks → bounded ``FileSegment``s →
``ReviewChunk``s (``chunking``) → per-chunk LLM review with retry/bisect
recovery and the map-phase cache (``mapping``) → false-positive verification
(each genuine finding is re-checked against the *whole* submission, since a
chunk reviewer saw only a slice, and confirmed false positives are dropped — see
``false_positive_filter``) → merged architecture-consistency + side-effect /
blast-radius pass (a single additive LLM call covering architecture
contradictions, cross-codebase redundancy, and caller-impact / documentation
mismatches the per-chunk view cannot see — see
``merged_architecture_side_effect_pass``) → side-effect consolidation (merges
related ``side-effects`` findings that share an enclosing construct or cite
one another — see ``side_effect_consolidation``) → deterministic merge (dedupe,
severity gate, safety nets) → optional post-dedupe spec-compliance synthesis.
When ``CODE_REVIEW_SPEC_COMPLIANCE_PASS`` is enabled for the ``CODE_REVIEW``
profile, each chunk's prompt omits the per-chunk ``acceptance_criteria``/
``spec_excerpt`` blocks (``architecture_overview`` is unaffected) and, after the
deterministic merge above, a single ``synthesize_spec_compliance`` call runs
over the final merged issue list; its note replaces the (now-empty) per-chunk
``spec_compliance_notes`` fed into ``synthesize_review_findings``, so a real
spec-compliance finding is synthesized once over the complete picture rather
than being silently dropped by per-chunk fast paths. The flag defaults off, in
which case behavior is unchanged. Every LLM call carries at most ``compute_code_review_map_chunk_chars`` of
code regardless of input size, and no input file is ever silently dropped:
empty files are named by info findings, and a chunk that cannot be reviewed
after recovery (retry, bisection, and a last-resort thinking-off retry) degrades
gracefully — by default its range is surfaced non-blockingly as
``CodeReviewOutput.not_reviewed_ranges`` (never posted as a PR comment, never
blocking) so the run completes over the chunks that succeeded, because a
reviewer-side hiccup is not a code defect. Setting
``CODE_REVIEW_BLOCK_ON_UNREVIEWED`` restores the legacy fail-closed behavior
where that range becomes a blocking ``high`` "not reviewed" finding and the
merged review is rejected. The run still fails loudly with
``CodeReviewUnavailableError`` for infrastructure failures (rate limit,
unreachable endpoint, auth/config) and when *no* chunk could be reviewed at all;
an unexpected error (a defect in the reviewer code, not a known LLM content
failure) propagates unchanged so it fails closed rather than being masked — the
review never renders an approving verdict on code it did not see.

This module owns the orchestration (``run_coordinator``) and the reduce phase
(dedupe, approval gate, narrative merge). The chunking transforms live in
``chunking`` and the map phase (per-chunk review, recovery, cache, sibling
surface) in ``mapping``; both are re-exported here so call sites and tests can
keep importing from ``coordinator``.

Map-phase cache: the review→fix→re-review loop re-invokes the whole coordinator
after every batch fix, but a fix only mutates the files that had issues, so most
chunks are byte-identical to the previous cycle. A process-global, bounded LRU
(``CODE_REVIEW_CHUNK_OUTCOME_CACHE_SIZE``) keyed on the chunk's exact LLM input
(``chunk.content`` + segment notes) plus a context fingerprint (the shared
task/spec/architecture/profile inputs and the resolved review model) reuses the
prior map-phase ``_ChunkOutcome`` for any unchanged chunk, so only chunks the
fix actually touched go back through the LLM. The cache is scoped to the map
phase only: the false-positive *verification* pass always re-runs on the current
submission (a finding can flip because a *different*, changed chunk altered
cross-file context, and verification reads the whole codebase), so no safety
guarantee is weakened. Only fully-reviewed outcomes are cached — degraded "not
reviewed" outcomes are never stored, so a transient failure is retried for real
next cycle. The cache is best-effort: a miss simply recomputes, so correctness
never depends on a hit, and any change to code, context, or model invalidates
the key.

Submission-level short-circuit: the map-phase cache still re-runs the reduce and
the false-positive *verification* pass on every cycle, so re-reviewing a
byte-identical submission that was already approved is not free. A second,
coarser process-global LRU (``CODE_REVIEW_SUBMISSION_CACHE_SIZE``) keyed on the
whole raw ``CodeReviewInput`` (files/code + task/spec/architecture context +
profile + resolved model) records the approved
``CodeReviewOutput`` of each submission, and ``run_coordinator`` returns a deep
clone of it before touching the LLM when the same submission comes back — zero
LLM calls (map, verification, and merge all skipped). Only approved outcomes are
stored: a rejection is left to re-run through the (cheap, mostly cached) map
phase so a fix that reappears identical still gets its findings. The key is
derived only from ``CodeReviewInput`` (plus the resolved model), so a verdict
that also depends on ``repo_reader`` (the false-positive filter's and the
architecture pass's whole-repository read access) cannot be safely keyed --
the rest of the repository can change between two byte-identical submissions
without changing the key. The short-circuit is therefore skipped entirely
(no read, no write) whenever a ``repo_reader`` is given, so a cached approval
can never mask a since-added architecture/redundancy finding or a
since-resolved false positive.

Cross-file surface: each chunk reviewer is also given the *sibling surface* —
the top-level symbols (Python ``def``/``class``, TS/JS ``export``s) defined by
the other changed files in the submission that are not in this chunk — so it can
flag a reference to a symbol a sibling renamed or removed, a cross-file break a
bounded single-chunk view would otherwise miss. That surface is folded into the
chunk's cache key, so a sibling's *surface* change (a rename/removal) re-runs the
dependent chunk with the new surface, while a body-only sibling edit leaves the
surface unchanged and the chunk stays cached — closing the cross-file gap without
invalidating the whole submission on every fix.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Callable, List, NamedTuple, Optional, Tuple

from llm_service import LLMClient, compact_text
from shared.env import env_flag_enabled
from shared.env_config import env_bool
from software_engineering_team.shared.context_sizing import (
    compute_code_review_arch_overview_chars,
    compute_code_review_existing_codebase_chars,
    compute_code_review_map_chunk_chars,
    compute_code_review_spec_excerpt_chars,
    parse_env_int,
)

from .architecture_context import render_architecture_context as _render_architecture_context
from .chunk_reviewer import ChunkReviewAgent
from .chunking import (
    MIN_SPLIT_SEGMENT_CHARS,
    _blocks_from_input,
    _issues_from_chunk_output,
    _map_parallelism,
    _normalize_issue_path,
    _segment_range_label,
    _validate_line,
    build_review_chunks,
    cap_chunk_content,
    cap_review_chunk,
    parse_code_into_file_blocks,
    split_block_into_segments,
)
from .false_positive_filter import CodebaseIndex, filter_false_positives
from .mapping import (
    _cached_review_chunk,
    _chunk_cache_key,
    _ChunkOutcome,
    _context_fingerprint,
    _is_content_failure,
    _is_infra_failure,
    _map_chunks,
    _review_chunk_with_recovery,
    _review_model_fingerprint,
    _sibling_surface,
    _stable_json_digest,
    _submission_fingerprint,
    _surface_by_path,
    _symbol_surface,
    clear_chunk_outcome_cache,
)
from .merged_architecture_side_effect_pass import find_architecture_and_side_effect_issues
from .models import (
    CodeReviewInput,
    CodeReviewIssue,
    CodeReviewOutput,
    CodeReviewUnavailableError,
    ReviewProgressCallback,
    notify_review_progress,
)
from .profiles import ReviewProfile
from .repo_reader import RepoReader
from .side_effect_consolidation import (
    SIDE_EFFECT_CONSOLIDATION_ENV as _SIDE_EFFECT_CONSOLIDATION_ENV,
)
from .side_effect_consolidation import (
    consolidate_side_effect_issues,
)
from .synthesis import synthesize_review_findings, synthesize_spec_compliance

logger = logging.getLogger(__name__)

# Names re-exported from ``chunking``/``mapping`` so existing call sites and
# tests can keep importing them from ``coordinator`` after the module split.
# Listing them here also marks the otherwise-unused imports above as public
# re-exports (so linters don't flag them).
__all__ = [
    "run_coordinator",
    "clear_submission_outcome_cache",
    "_submission_fingerprint",
    "MIN_SPLIT_SEGMENT_CHARS",
    "parse_code_into_file_blocks",
    "split_block_into_segments",
    "build_review_chunks",
    "cap_chunk_content",
    "cap_review_chunk",
    "clear_chunk_outcome_cache",
    "_blocks_from_input",
    "_issues_from_chunk_output",
    "_map_parallelism",
    "_normalize_issue_path",
    "_segment_range_label",
    "_validate_line",
    "_ChunkOutcome",
    "_cached_review_chunk",
    "_chunk_cache_key",
    "_context_fingerprint",
    "_is_content_failure",
    "_is_infra_failure",
    "_map_chunks",
    "_review_chunk_with_recovery",
    "_review_model_fingerprint",
    "_sibling_surface",
    "_stable_json_digest",
    "_surface_by_path",
    "_symbol_surface",
]

# Process-global submission-level short-circuit cache (see module docstring's
# "Submission-level short-circuit" section). ``0`` disables it (every run is a
# guaranteed miss).
DEFAULT_SUBMISSION_CACHE_SIZE = 256  # CODE_REVIEW_SUBMISSION_CACHE_SIZE, floor 0

# Named so ``run_coordinator`` (the sole reader) and this module's docstrings/tests
# never risk a typo'd duplicate literal; mirrors ``SIDE_EFFECT_CONSOLIDATION_ENV``'s
# module-level-constant pattern.
CODE_REVIEW_SPEC_COMPLIANCE_PASS_ENV = "CODE_REVIEW_SPEC_COMPLIANCE_PASS"

# Progress-bar checkpoints (0.0-1.0), in the order the review actually reaches them:
# preparing input -> chunking done (also the map phase's start -- see
# mapping.py's _MAP_PHASE_START, which must stay equal to this) -> per-chunk map
# review (reported incrementally by mapping.py, not here) -> verifying -> finalizing -> done.
_PROGRESS_PREPARING_INPUT = 0.05
_PROGRESS_CHUNKING_DONE = 0.10
_PROGRESS_VERIFYING = 0.92
_PROGRESS_FINALIZING = 0.95
_PROGRESS_DONE = 1.0

_SUBMISSION_OUTCOME_CACHE: "OrderedDict[str, CodeReviewOutput]" = OrderedDict()
_SUBMISSION_OUTCOME_CACHE_LOCK = threading.Lock()


def _submission_cache_size() -> int:
    """Resolve the submission cache capacity from the environment.

    Postconditions:
        - Returns ``CODE_REVIEW_SUBMISSION_CACHE_SIZE`` parsed as an int,
          clamped to a floor of 0: an unset or unparseable value falls back to
          the default, while a negative value is clamped to 0 (not the
          default) — same as any other floor. An explicit or clamped-to 0
          disables the short-circuit; ``0`` is load-bearing: callers treat it
          as "no submission cache", so every review runs in full.
        - The return value is always a non-negative ``int`` (never ``None``,
          never negative, never a non-int): ``parse_env_int`` never raises for
          a hostile environment value and always clamps to the given floor
          before returning, so callers (e.g. ``run_coordinator``'s eviction
          loop) may treat this value as pre-validated and need not re-check it.
    """
    return parse_env_int("CODE_REVIEW_SUBMISSION_CACHE_SIZE", DEFAULT_SUBMISSION_CACHE_SIZE, 0)


def clear_submission_outcome_cache() -> None:
    """Drop every cached approved submission outcome.

    Postconditions:
        - The process-global submission cache is empty; the next review of any
          submission is a guaranteed miss. Intended for tests (the cache persists
          across ``run_coordinator`` calls by design) and for callers that must
          force a cold review.
    """
    with _SUBMISSION_OUTCOME_CACHE_LOCK:
        _SUBMISSION_OUTCOME_CACHE.clear()


def _block_on_unreviewed() -> bool:
    """Whether a chunk that could not be reviewed should block the merged review.

    Postconditions:
        - Returns ``True`` only when ``CODE_REVIEW_BLOCK_ON_UNREVIEWED`` is an
          explicit truthy value (``true``/``1``/``yes``/``on``); unset or
          anything else is ``False`` (the default — see module docstring for
          why default-off is preferred and what setting it restores).
    """
    return env_bool("CODE_REVIEW_BLOCK_ON_UNREVIEWED", default=False)


def _not_reviewed_range_label(issue: CodeReviewIssue) -> str:
    """Render a not-reviewed coverage finding as a concise ``path (lines A-B)`` label.

    Postconditions:
        - Returns ``"<path> (lines <start>-<end>)"`` when the finding carries a
          line range, ``"<path>"`` when it does not, and ``"(unknown)"`` for a
          headerless finding with no path. Pure formatting for the non-blocking
          ``not_reviewed_ranges`` observability list; never raises.
    """
    path = issue.file_path or "(unknown)"
    if issue.start_line is not None and issue.line is not None:
        return f"{path} (lines {issue.start_line}-{issue.line})"
    return path


def _tail_passes_run_sequentially(llm: LLMClient) -> bool:
    """True when the coordinator's tail passes must run one at a time.

    Scripted ``DummyLLMClient`` doubles use a shared non-thread-safe response index,
    so they are not safe under concurrent fan-out. Mirrors
    ``shared.v2_review._review_steps_run_sequentially``; both delegate to the shared
    ``is_dummy_llm_client_wrapped`` helper (unwraps a Strands ``LLMClientModel``
    wrapper before checking) so the detection logic lives in one place.

    Preconditions: ``llm`` is the LLM client that will be handed to the tail-pass thunks.
    Postconditions: returns ``True`` iff ``llm`` is (or wraps) a ``DummyLLMClient``. Pure.
    """
    from llm_service.clients.dummy import is_dummy_llm_client_wrapped

    return is_dummy_llm_client_wrapped(llm)


def _dedupe_issues(all_issues: List[CodeReviewIssue]) -> List[CodeReviewIssue]:
    """Dedupe issues by (file_path, line, description).

    Line is part of an issue's identity now that it anchors inline PR comments,
    so the same description on two different lines is two distinct findings,
    not a duplicate. An unanchored copy (line=None) of a finding that also
    appears anchored (same file_path+description) is dropped in favour of the
    anchored one, so the issue isn't reported twice (once in the body, once
    inline).

    Postconditions:
        - Order of first occurrence is preserved.
    """
    anchored_pairs = {(i.file_path, i.description) for i in all_issues if i.line is not None}
    seen: set[Tuple[str, Optional[int], str]] = set()
    deduped: List[CodeReviewIssue] = []
    for issue in all_issues:
        if issue.line is None and (issue.file_path, issue.description) in anchored_pairs:
            continue
        key = (issue.file_path, issue.line, issue.description)
        if key not in seen:
            seen.add(key)
            deduped.append(issue)
    return deduped


# Hard ceiling on findings returned by one review. Applied after dedupe and
# before the approval gate so approval and narrative synthesis see the same
# capped list. Severity-first ranking keeps blocking findings ahead of nits.
MAX_CODE_REVIEW_ISSUES = 30

# Mirrors synthesis._SEVERITY_RANK so the reduce-phase cap and the findings
# digest agree on presentation order (critical → high → medium → low → info).
_CAP_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_CAP_UNKNOWN_SEVERITY_RANK = len(_CAP_SEVERITY_RANK)


def _cap_issues(
    issues: List[CodeReviewIssue],
    limit: int = MAX_CODE_REVIEW_ISSUES,
) -> List[CodeReviewIssue]:
    """Keep at most ``limit`` issues, ranked severity-first (stable within rank).

    Preconditions:
        - ``issues`` is the deduped merged finding list (may be empty).
        - ``limit`` is a non-negative integer.

    Postconditions:
        - Returns a new list of length ``min(len(issues), limit)``.
        - When truncation occurs, ordering is ``critical → high → medium →
          low → info`` (unknown severities last), preserving input order
          within the same severity so blocking findings are never dropped
          in favour of nits.
        - When ``len(issues) <= limit``, returns a shallow copy in the
          original order (no re-sort).
        - Never mutates ``issues``.
    """
    assert limit >= 0, "limit must be non-negative"
    if len(issues) <= limit:
        return list(issues)
    ordered = sorted(
        enumerate(issues),
        key=lambda pair: (
            _CAP_SEVERITY_RANK.get(
                (pair[1].severity or "").strip().lower(), _CAP_UNKNOWN_SEVERITY_RANK
            ),
            pair[0],
        ),
    )
    capped = [issue for _, issue in ordered[:limit]]
    logger.info(
        "CodeReview: capped issues %s -> %s (severity-first)",
        len(issues),
        limit,
    )
    return capped


def _reconcile_approval(
    llm_approved: bool,
    issues: List[CodeReviewIssue],
) -> Tuple[bool, List[CodeReviewIssue]]:
    """Deterministic approval gate with the anti-loop safety nets.

    Preconditions:
        - ``issues`` is the deduped (and typically severity-capped) merged
          issue list. Any rejecting sub-review's summary has already been
          synthesized into a high issue per sub-review
          (``_review_chunk_with_recovery``), so issue text and verdicts are
          correctly paired before they reach this gate.

    Postconditions:
        - ``approved is False`` implies the returned issues contain at least
          one critical/high finding (rejections are always actionable).
        - A reject with only minor/info issues, or with no actionable feedback
          at all, flips to approve. The merged summary is never consulted here:
          it mixes every chunk's text, so synthesizing a rejection from it
          could attribute an approving chunk's words to a rejecting chunk.
    """
    critical_or_high = [i for i in issues if i.severity in ("critical", "high")]
    approved = llm_approved and not critical_or_high
    if not approved and not critical_or_high:
        if issues:
            logger.info(
                "CodeReview: overriding to approved=True (only %s minor/nit issues, no critical/high)",
                len(issues),
            )
        else:
            logger.warning(
                "CodeReview: LLM rejected with no issues and no actionable feedback -- "
                "auto-approving (nothing to give the coding agent)"
            )
        approved = True
    return approved, issues


def _merge_narrative(
    llm: LLMClient,
    input_data: CodeReviewInput,
    approved: bool,
    issues: List[CodeReviewIssue],
    outcome: "_ChunkOutcome",
    has_additive_pass_findings: bool = False,
    single_pass_spec_notes: Optional[str] = None,
) -> Tuple[str, str]:
    """Produce the merged ``(summary, spec_compliance_notes)`` for the review.

    The reduce phase's narrative — never the verdict, which is already fixed.

    Preconditions:
        - ``approved`` and ``issues`` are the authoritative deterministic
          results from ``_reconcile_approval``; this function only shapes prose
          and never reconsults or mutates them.
        - ``outcome.summaries`` holds one entry per successful sub-review.
        - ``has_additive_pass_findings`` is True when the architecture-consistency
          pass and/or the side-effect-impact pass (both of which run outside the
          map phase) added findings not reflected in any ``outcome.summaries``
          entry.
        - ``single_pass_spec_notes`` is ``None`` when ``CODE_REVIEW_SPEC_COMPLIANCE_PASS``
          is off (or profile-gated off, or the dedicated pass failed); otherwise it is
          the ``synthesize_spec_compliance`` result (possibly ``""`` for "no gaps found")
          that replaces every per-chunk ``spec_compliance_notes`` entry, since the
          per-chunk prompts omitted spec/acceptance-criteria context in that mode.

    Postconditions:
        - When ``single_pass_spec_notes`` is ``None`` and there's exactly one
          sub-review with no additive-pass findings, returns that sub-review's
          summary/notes verbatim and makes no synthesis LLM call — unchanged from
          today's behavior.
        - When ``single_pass_spec_notes`` is not ``None``, the single-chunk fast
          path is never taken (even for one chunk) so the dedicated pass's note is
          never silently dropped; the synthesis call is fed
          ``chunk_spec_notes=[single_pass_spec_notes]`` in place of
          ``outcome.spec_notes``, and the concatenation fallback is
          ``single_pass_spec_notes`` directly.
        - Otherwise attempts a single findings-only synthesis pass so the
          narrative reflects every source of ``issues`` (including the
          architecture and side-effect passes); on any failure (``None``) falls
          back to the ``"\\n\\n"``-joined per-pass summaries/notes.
    """
    if single_pass_spec_notes is None:
        if len(outcome.summaries) == 1 and not has_additive_pass_findings:
            return outcome.summaries[0], (outcome.spec_notes[0] if outcome.spec_notes else "")
        concatenated_summary = "\n\n".join(s for s in outcome.summaries if s.strip())
        concatenated_notes = "\n\n".join(n for n in outcome.spec_notes if n.strip())
        chunk_spec_notes = outcome.spec_notes
    else:
        concatenated_summary = "\n\n".join(s for s in outcome.summaries if s.strip())
        concatenated_notes = single_pass_spec_notes
        chunk_spec_notes = [single_pass_spec_notes]

    synthesized = synthesize_review_findings(
        llm,
        input_data=input_data,
        approved=approved,
        issues=issues,
        chunk_summaries=outcome.summaries,
        chunk_spec_notes=chunk_spec_notes,
    )
    if synthesized is not None:
        return synthesized.summary, synthesized.spec_compliance_notes

    return concatenated_summary, concatenated_notes


class _TailPassResult(NamedTuple):
    """Merged output of :func:`_run_tail_passes`.

    ``issues`` is the single ordered, merged findings list; ``has_additive_findings``
    is carried separately (not re-derivable from ``issues`` alone once merged)
    because the caller needs it standalone to decide whether the narrative
    synthesis pass must run even for a single-chunk review (see
    ``_merge_narrative``'s ``has_additive_pass_findings`` parameter).
    """

    issues: List[CodeReviewIssue]
    has_additive_findings: bool


def _run_tail_passes(
    *,
    llm: LLMClient,
    input_data: CodeReviewInput,
    genuine_issues: List[CodeReviewIssue],
    repo_reader: Optional[RepoReader],
    shared_index: CodebaseIndex,
) -> _TailPassResult:
    """Run the false-positive filter and the merged architecture/side-effect pass.

    Both are once-per-submission, read-only checks over the same shared
    ``CodebaseIndex`` (built once by the caller): false-positive verification
    re-checks each genuine chunk finding against the whole submission and drops
    confirmed false positives; the merged pass runs architecture-consistency and
    side-effect / blast-radius checks in a single LLM call and returns the two
    finding lists separately. Neither reads the other's output, so they are
    independent of call order.

    Preconditions:
        - ``genuine_issues`` is the deduped set of genuine chunk findings
          (coverage/safety findings excluded, per ``filter_false_positives``'s
          own precondition).
        - ``shared_index`` was built from the same ``input_data``/``repo_reader``.

    Postconditions:
        - When ``input_data.skip_tail_passes`` is set, neither pass runs (no LLM
          calls at all): returns a :class:`_TailPassResult` whose ``issues`` is
          ``genuine_issues`` unchanged and whose ``has_additive_findings`` is
          always False. This is a strict superset of
          ``skip_false_positive_filter``'s effect (setting both is redundant,
          not conflicting) — a lightweight mode for a fallback caller that
          wants speed over full tail-pass rigor.
        - Otherwise, returns a :class:`_TailPassResult` whose ``issues`` is the
          false-positive-filtered (or, when ``input_data.skip_false_positive_filter``
          is set, unfiltered) ``genuine_issues``, followed by the architecture
          findings, followed by the side-effect findings — the same order the
          caller's merge produced before this fan-out existed — and whose
          ``has_additive_findings`` is True iff either half of the merged pass
          contributed at least one finding.
        - When ``llm`` is (or wraps) a ``DummyLLMClient`` (see
          ``_tail_passes_run_sequentially``), fewer than two passes are
          scheduled, or ``_map_parallelism()`` resolves to <= 1, the passes run
          sequentially. Otherwise they run concurrently via ``parallel_map``.
    """
    if input_data.skip_tail_passes:
        return _TailPassResult(issues=genuine_issues, has_additive_findings=False)

    calls: List[Tuple[str, Callable[[], object]]] = []
    if not input_data.skip_false_positive_filter:
        calls.append(
            (
                "filter",
                lambda: filter_false_positives(
                    llm, input_data, genuine_issues, repo_reader=repo_reader, index=shared_index
                ),
            )
        )
    calls.append(
        (
            "merged",
            lambda: find_architecture_and_side_effect_issues(
                llm, input_data, repo_reader=repo_reader, index=shared_index
            ),
        )
    )

    if _tail_passes_run_sequentially(llm) or len(calls) <= 1 or _map_parallelism() <= 1:
        results = {name: fn() for name, fn in calls}
    else:
        # Imported lazily, matching shared/v2_review.py's and
        # shared/phases/review_cycle.py's identical parallel_map import — keeps
        # the module import light for callers that never hit the concurrent
        # branch (e.g. every DummyLLMClient-backed test).
        from shared.concurrency import parallel_map

        outputs = parallel_map(
            [fn for _, fn in calls], lambda fn: fn(), max_workers=len(calls), skip_none=False
        )
        results = {name: output for (name, _), output in zip(calls, outputs)}

    verified = results.get("filter", genuine_issues)
    architecture_findings, side_effect_findings = results["merged"]
    if architecture_findings:
        verified = [*verified, *architecture_findings]
    if side_effect_findings:
        verified = [*verified, *side_effect_findings]
    return _TailPassResult(
        issues=verified,
        has_additive_findings=bool(architecture_findings) or bool(side_effect_findings),
    )


def run_coordinator(
    llm: LLMClient,
    input_data: CodeReviewInput,
    progress_callback: Optional[ReviewProgressCallback] = None,
    repo_reader: Optional[RepoReader] = None,
) -> CodeReviewOutput:
    """Map-reduce review entry point: bounded chunks in, merged verdict out.

    Preconditions:
        - ``llm`` implements ``LLMClient`` (context sizing + chunk review calls)
          and, unless it is (or wraps) a ``DummyLLMClient``, must tolerate
          concurrent calls from worker threads: the map phase already fans
          chunk reviews out (see ``_map_chunks``), and the false-positive /
          merged architecture/side-effect tail pass fan out
          the same
          way (see ``_run_tail_passes``) unless ``_tail_passes_run_sequentially(llm)``
          returns True or the ``CODE_REVIEW_MAP_PARALLELISM`` budget resolves to
          <= 1, either of which forces the sequential fallback instead. The
          central ``llm_service`` clients already guard their shared state
          internally for this.
        - ``input_data`` carries the code under review via ``files`` or ``code``.
        - ``progress_callback`` is None or satisfies the
          ``ReviewProgressCallback`` contract (non-raising, accepts
          ``(step, detail, fraction)``).
        - ``repo_reader`` is None or a ``repo_reader.RepoReader`` (read-only,
          thread-safe, fail-safe): whole-repo read access handed to the
          false-positive verifier so it can confirm that a file/module a finding
          claims is missing already exists outside the diff. Passed as an
          argument (never a ``CodeReviewInput`` field) so the live reader object
          can never enter the submission/chunk cache keys.

    Postconditions:
        - Every input file/line range is either reviewed or named: empty files
          get info findings, and a chunk that cannot be reviewed after recovery
          is recorded in ``not_reviewed_ranges`` while the run completes over the
          chunks that succeeded (no covered line is silently dropped). By default
          those ranges are non-blocking (never posted, never affecting
          ``approved``); under ``CODE_REVIEW_BLOCK_ON_UNREVIEWED`` they instead
          appear as blocking ``high`` findings in ``issues`` and reject the merge,
          so unreviewed code cannot pass the gate as approved.
        - ``approved is False`` implies at least one critical/high issue.
        - Every genuine reviewer finding is re-checked against the whole
          submission (see ``false_positive_filter``'s module docstring for why)
          and dropped only when the verifier confirms it is a false positive;
          when that removes the last critical/high finding the gate approves (a
          chunk-local false positive never blocks the merge). The check is
          fail-safe — any verifier failure keeps the findings — and never
          touches the not-reviewed coverage findings. This pass and the merged
          architecture/side-effect pass below it — their concurrency/fallback
          scheduling, finding-list order, and ``skip_tail_passes`` behavior —
          are exactly as documented on ``_run_tail_passes``, which this
          function calls unchanged.
        - After the false-positive filter and the merged additive pass, related
          ``side-effects`` findings may be optionally consolidated (gated by
          ``CODE_REVIEW_SIDE_EFFECT_CONSOLIDATION``; fail-safe on error — see
          the consolidation step in the body) before the deterministic
          dedupe/severity gate.
        - When ``CODE_REVIEW_SPEC_COMPLIANCE_PASS`` is enabled and
          ``input_data.profile`` is ``ReviewProfile.CODE_REVIEW``, every chunk's
          prompt omits the ``acceptance_criteria``/``spec_excerpt`` blocks
          (``architecture_overview`` is unaffected), and after the deterministic
          dedupe/severity gate above, ``synthesize_spec_compliance`` is called
          exactly once over the final merged issue list; its note replaces the
          per-chunk ``spec_compliance_notes`` passed into
          ``synthesize_review_findings``. If that call raises, the failure is
          logged and narrative merge falls back to per-chunk-sourced notes
          (``single_pass_spec_notes`` left ``None``). When the flag is off (the
          default) or the profile is not ``CODE_REVIEW``, behavior is unchanged:
          every chunk gets its per-chunk spec/AC context and
          ``synthesize_spec_compliance`` is never called.
        - The code under review is never compacted or truncated; only the
          spec/architecture/existing-codebase excerpts are.
        - A submission byte-identical to one this process already approved *and
          fully reviewed* (same code + context + model + output-affecting
          toggles including ``CODE_REVIEW_SIDE_EFFECT_CONSOLIDATION`` and
          ``CODE_REVIEW_SPEC_COMPLIANCE_PASS``; no unreviewed ranges) returns
          the recorded approved output with no LLM call at all — unless a
          ``repo_reader`` is given, in which case this
          short-circuit never fires (see module docstring's "Submission-level
          short-circuit" section for why). The cache-hit check, its LRU touch,
          and the deep clone of the served output all happen under a single
          ``_SUBMISSION_OUTCOME_CACHE_LOCK`` acquisition, so a concurrent
          write-back (see below) can never interleave with a hit being read;
          the lock is released before ``progress_callback`` runs, since
          caller-supplied code must never execute while this process-global,
          non-reentrant lock is held.
        - When ``progress_callback`` is provided, it is invoked with
          non-decreasing fractions ending at 1.0 (step ``done``) on every
          successful return, including per-chunk ``reviewing`` reports.
        - The total number of concurrent chunk-review ``reviewer.run()`` calls
          for this run — top-level chunks and every concurrently in-flight
          bisection-recovery half combined — never exceeds ``_map_parallelism()``:
          a semaphore sized to that budget is created once here and threaded
          through the map phase (see ``_map_chunks``'s ``run_limiter``), so
          ``CODE_REVIEW_MAP_PARALLELISM`` is a true run-wide ceiling in thread
          mode, not just a bound on the outer per-chunk fan-out width. Temporal
          mode reviews each chunk via an independent, stateless
          ``review_chunk_activity`` invocation with no such object shared across
          activities, so this ceiling does not apply there — see
          ``docs/ENV_VARS.md``'s ``CODE_REVIEW_MAP_PARALLELISM`` entry for its
          Temporal-mode bound.

    Raises:
        CodeReviewUnavailableError: when the review model is unavailable
            (an infrastructure failure: rate limit, unreachable endpoint, or
            auth/config error), or when *no* chunk could be reviewed at all —
            the run never renders a verdict on a submission it did not see. In
            the latter case, ``unreviewed`` names only the not-reviewed range
            labels recorded before the failure — never genuine reviewer
            findings, none of which can exist in this branch (a chunk that
            fails contributes no ``issues`` entry; see ``_ChunkOutcome``).
        Exception: an unexpected reviewer defect (not a known LLM content
            failure) propagates unchanged, failing closed so the bug surfaces
            instead of being masked as a not-reviewed finding.
    """
    # Resolve the review model once for the whole run: it feeds both the
    # submission fingerprint here and the map-phase context fingerprint below, and
    # is identical throughout (best-effort identity, never raises).
    model_fingerprint = _review_model_fingerprint(llm)

    # Computed once per run (never re-read per chunk or per fingerprint call) so
    # every chunk's prompt, the submission fingerprint below, and the post-dedupe
    # single-pass call later all agree on the same decision. Restricted to
    # CODE_REVIEW, matching every sibling tail pass's profile restriction -- a
    # profile-blind flag read would omit per-chunk spec/AC context on other
    # profiles without the post-dedupe pass ever running to replace it, and would
    # also fingerprint non-CODE_REVIEW submissions as flag-sensitive when they
    # never actually are, causing needless cache misses whenever the env var
    # happens to be set.
    spec_compliance_single_pass = env_bool(
        CODE_REVIEW_SPEC_COMPLIANCE_PASS_ENV, default=False
    ) and (input_data.profile == ReviewProfile.CODE_REVIEW)

    # Submission-level short-circuit (see module docstring's "Submission-level
    # short-circuit" section for the full rationale). On a miss the run proceeds
    # and stores its verdict below if approved.
    submission_size = _submission_cache_size()
    submission_key: Optional[str] = None
    cached: Optional[CodeReviewOutput] = None
    if submission_size > 0 and repo_reader is None:
        submission_key = _submission_fingerprint(
            input_data, model_fingerprint, spec_compliance_single_pass
        )
        with _SUBMISSION_OUTCOME_CACHE_LOCK:
            hit = _SUBMISSION_OUTCOME_CACHE.get(submission_key)
            if hit is not None:
                _SUBMISSION_OUTCOME_CACHE.move_to_end(submission_key)
                # Clone while still locked so the served copy is independent of
                # the cache entry; done here (not after release) keeps the
                # dict read + LRU touch + clone as one atomic critical section.
                cached = hit.model_copy(deep=True)
        if cached is not None:
            # Released the lock before this point: progress_callback is
            # caller-supplied and may be slow (e.g. a synchronous job-service
            # update) or re-entrant (e.g. it calls clear_submission_outcome_cache()
            # or run_coordinator() again) -- either would deadlock against this
            # process-global, non-reentrant lock if still held here.
            logger.info("CodeReviewCoordinator: submission cache hit; skipping review (approved)")
            notify_review_progress(
                progress_callback,
                "done",
                "identical approved submission; review skipped",
                _PROGRESS_DONE,
            )
            return cached

    notify_review_progress(
        progress_callback, "preparing", "preparing review input", _PROGRESS_PREPARING_INPUT
    )
    blocks, skipped_empty = _blocks_from_input(input_data)
    skipped_issues = [
        CodeReviewIssue(
            severity="info",
            category="general",
            file_path=path,
            description="File content is empty or whitespace-only; nothing to review.",
            suggestion="Confirm the file is intentionally empty.",
        )
        for path in skipped_empty
    ]
    if not blocks:
        notify_review_progress(progress_callback, "done", "no code to review", _PROGRESS_DONE)
        return CodeReviewOutput(
            approved=True,
            issues=skipped_issues,
            summary="No code to review.",
            spec_compliance_notes="",
        )

    max_spec = compute_code_review_spec_excerpt_chars(llm)
    max_arch = compute_code_review_arch_overview_chars(llm)
    max_existing = compute_code_review_existing_codebase_chars(llm)
    # Hard caps after compaction: compact_text returns the original text when
    # its LLM call fails, so the slice is what actually guarantees the chunk
    # reviewer's bounded-prompt precondition.
    spec_content = compact_text(input_data.spec_content or "", max_spec, llm, "specification")[
        :max_spec
    ]
    arch_overview = ""
    if input_data.architecture:
        arch_overview = compact_text(
            _render_architecture_context(input_data.architecture),
            max_arch,
            llm,
            "architecture overview",
        )[:max_arch]
    existing_codebase = compact_text(
        input_data.existing_codebase or "", max_existing, llm, "existing codebase"
    )[:max_existing]

    chunks = build_review_chunks(
        blocks, compute_code_review_map_chunk_chars(llm), input_data.pre_numbered
    )
    logger.info(
        "CodeReviewCoordinator: %s blocks -> %s chunks",
        len(blocks),
        len(chunks),
    )
    notify_review_progress(
        progress_callback, "preparing", f"split into {len(chunks)} chunks", _PROGRESS_CHUNKING_DONE
    )

    base_input = {
        "language": input_data.language or "",
        "task_description": input_data.task_description or "",
        "task_requirements": input_data.task_requirements or "",
        "acceptance_criteria": input_data.acceptance_criteria or [],
        "spec_excerpt": spec_content,
        "architecture_overview": arch_overview,
        "existing_codebase_excerpt": existing_codebase or None,
        "user_decisions": input_data.user_decisions or None,
        "profile": input_data.profile,
        "spec_compliance_single_pass": spec_compliance_single_pass,
    }

    # Fingerprint the shared context + resolved model once per run so unchanged
    # chunks reuse their prior map-phase outcome (see module docstring). Computed
    # here (not per chunk) because it is identical for every chunk in this run.
    context_fp = _context_fingerprint(base_input, model_fingerprint)

    # Cross-file sibling surface (see module docstring's "Cross-file surface"
    # section).
    surface_by_path = _surface_by_path(blocks)

    chunk_reviewer = ChunkReviewAgent(llm)
    outcome = _ChunkOutcome()
    # Review-run-scoped concurrency ceiling (see this function's docstring's
    # "total number of concurrent chunk-review calls" postcondition, and
    # mapping.py's ``_run_reviewer_call``/``_map_chunks`` docstrings for how
    # it's honored down the call chain).
    run_limiter = threading.Semaphore(_map_parallelism())
    for per_chunk in _map_chunks(
        chunk_reviewer,
        chunks,
        base_input,
        context_fp,
        surface_by_path,
        progress_callback,
        run_limiter=run_limiter,
    ):
        outcome.absorb(per_chunk)

    # Total-failure guard: individual chunks degrade gracefully to a
    # not-reviewed finding, but a run in which *no* chunk produced a verdict
    # has reviewed nothing — rendering approved/rejected here would be a
    # verdict on code we never saw. Fail loudly instead, naming what went
    # unreviewed (the degraded not-reviewed findings already record the ranges).
    if not outcome.approved_flags:
        raise CodeReviewUnavailableError(
            "No chunk could be reviewed after recovery; no verdict was produced for this submission.",
            unreviewed=[i.description for i in outcome.not_reviewed_issues],
        )

    # False-positive verification (see ``false_positive_filter``'s module
    # docstring for the full rationale). Coverage/safety findings
    # (``not_reviewed_issues``, empty-file notices) are never passed in, so the
    # gate's anti-loop nets stay intact.
    genuine_issues = _dedupe_issues(outcome.issues)
    notify_review_progress(
        progress_callback,
        "verifying",
        f"verifying {len(genuine_issues)} findings against the full codebase",
        _PROGRESS_VERIFYING,
    )
    # Built once and shared with the false-positive filter, the merged
    # architecture/side-effect pass, and the side-effect consolidation step
    # below: all read the same submission/repo_reader,
    # so a single index avoids parsing the submission twice.
    # CodebaseIndex is read-only after construction (see its own docstring's Invariants),
    # so this one instance is safe to hand to the tail passes when they run
    # concurrently in worker threads (see ``_run_tail_passes``).
    shared_index = CodebaseIndex.from_input(input_data, repo_reader=repo_reader)

    # See ``_run_tail_passes`` for the false-positive filter / merged
    # architecture-side-effect pass split, scheduling, and skip behavior.
    # ``skip_false_positive_filter`` is for a gate whose findings must never be
    # silently dropped; skipping only removes the drop-false-positives step, so
    # it can only ever keep more findings. After those passes, related
    # ``side-effects`` findings may optionally be consolidated (gated by
    # ``CODE_REVIEW_SIDE_EFFECT_CONSOLIDATION``; fail-safe on error) before the
    # same dedupe/severity-gate/merge machinery below. The merged halves are
    # restricted internally to the default CODE_REVIEW profile -- see their own
    # docstrings for why the other profiles must never receive these findings.
    tail_pass_result = _run_tail_passes(
        llm=llm,
        input_data=input_data,
        genuine_issues=genuine_issues,
        repo_reader=repo_reader,
        shared_index=shared_index,
    )
    tail_pass_issues = tail_pass_result.issues
    # Merge related "side-effects" findings (same enclosing function, or one
    # citing another's) into single consolidated issues before the exact-match
    # dedupe below -- see side_effect_consolidation's own docstring for the
    # grouping rules. Additive-only inputs in, fewer-but-richer issues out;
    # every other category passes through untouched.
    if env_flag_enabled(_SIDE_EFFECT_CONSOLIDATION_ENV):
        try:
            tail_pass_issues = consolidate_side_effect_issues(tail_pass_issues, shared_index)
        except Exception:
            logger.exception(
                "CodeReviewCoordinator: side-effect consolidation failed; "
                "using unconsolidated tail-pass issues"
            )

    notify_review_progress(
        progress_callback,
        "finalizing",
        "deduplicating findings and applying approval rules",
        _PROGRESS_FINALIZING,
    )
    # Degrade gracefully for an unreviewable chunk (see module docstring and
    # ``_block_on_unreviewed``); surfaced non-blockingly below and in the
    # telemetry log by default.
    not_reviewed_ranges: List[str] = [
        _not_reviewed_range_label(i) for i in outcome.not_reviewed_issues
    ]
    if _block_on_unreviewed():
        deduped = _dedupe_issues([*tail_pass_issues, *outcome.not_reviewed_issues, *skipped_issues])
    else:
        if not_reviewed_ranges:
            logger.warning(
                "CodeReview: %s chunk range(s) could not be reviewed; degrading gracefully "
                "(not posting/blocking; ranges=%s)",
                len(not_reviewed_ranges),
                not_reviewed_ranges,
            )
        deduped = _dedupe_issues([*tail_pass_issues, *skipped_issues])
    deduped = _cap_issues(deduped)
    # outcome.approved_flags is non-empty here: the total-failure guard above
    # already raised otherwise, and nothing between there and here mutates it.
    all_llm_approved = all(outcome.approved_flags)
    approved, deduped = _reconcile_approval(all_llm_approved, deduped)

    # CODE_REVIEW_SPEC_COMPLIANCE_PASS: run the dedicated single pass once, over
    # the final deduped issue list, instead of relying on the (now-empty)
    # per-chunk spec_compliance_notes. ``spec_compliance_single_pass`` already
    # folds in the CODE_REVIEW profile restriction (see its computation above).
    # ``None`` (flag/profile off, or the pass itself failed) tells
    # ``_merge_narrative`` to fall back to today's per-chunk-sourced behavior
    # unchanged.
    single_pass_spec_notes: Optional[str] = None
    if spec_compliance_single_pass:
        try:
            single_pass_spec_notes = synthesize_spec_compliance(
                llm, input_data=input_data, issues=deduped
            )
        except Exception:
            logger.exception(
                "CodeReviewCoordinator: spec-compliance single pass failed; falling back"
            )
            single_pass_spec_notes = None

    merged_summary, spec_notes = _merge_narrative(
        llm,
        input_data,
        approved,
        deduped,
        outcome,
        has_additive_pass_findings=tail_pass_result.has_additive_findings,
        single_pass_spec_notes=single_pass_spec_notes,
    )

    logger.info(
        "CodeReviewCoordinator: done, approved=%s, issues=%s, chunks=%s (sub-reviews=%s)",
        approved,
        len(deduped),
        len(chunks),
        len(outcome.approved_flags),
    )

    notify_review_progress(
        progress_callback, "done", f"approved={approved}, issues={len(deduped)}", _PROGRESS_DONE
    )
    result = CodeReviewOutput(
        approved=approved,
        issues=deduped,
        not_reviewed_ranges=not_reviewed_ranges,
        summary=merged_summary,
        spec_compliance_notes=spec_notes,
    )
    # Record only approved verdicts for the submission-level short-circuit (see
    # module docstring). A run that left any range unreviewed is also not
    # stored: freezing it would keep serving a partial verdict on later identical
    # cycles instead of re-attempting the chunk that could not be reviewed (a
    # semantic-exhaustion/truncation hiccup may not recur), matching the
    # map-phase rule that degraded chunk outcomes are never cached. Store a
    # clone so a later hit can be mutated freely without corrupting the entry.
    if submission_key is not None and result.approved and len(not_reviewed_ranges) == 0:
        with _SUBMISSION_OUTCOME_CACHE_LOCK:
            _SUBMISSION_OUTCOME_CACHE[submission_key] = result.model_copy(deep=True)
            _SUBMISSION_OUTCOME_CACHE.move_to_end(submission_key)
            # submission_size is a validated non-negative int by
            # _submission_cache_size()'s postcondition — no re-check needed here.
            while len(_SUBMISSION_OUTCOME_CACHE) > submission_size:
                _SUBMISSION_OUTCOME_CACHE.popitem(last=False)
    return result
