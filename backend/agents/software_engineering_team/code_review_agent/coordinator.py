"""Code Review Coordinator: map-reduce review with bounded per-call prompts.

Pipeline: input → (path, content) blocks → bounded ``FileSegment``s →
``ReviewChunk``s (``chunking``) → per-chunk LLM review with retry/bisect
recovery and the map-phase cache (``mapping``) → merged architecture-consistency
+ side-effect / blast-radius pass (a single additive LLM call covering
architecture contradictions, cross-codebase redundancy, and caller-impact /
documentation mismatches the per-chunk view cannot see — see
``merged_architecture_side_effect_pass``) → finding combination (proximity +
same-anchor similarity, subsuming the exact-match dedupe and the side-effect
consolidation — see ``finding_combination``) → false-positive verification over
the combined set (each finding, including the additive ones, re-checked against
the *whole* submission since a chunk reviewer saw only a slice, and confirmed
false positives dropped — see ``false_positive_filter``) → deterministic merge
(fold in coverage findings, severity gate, safety nets) → optional post-dedupe
spec-compliance synthesis.
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
chunks are byte-identical to the previous cycle. A shared, bounded LRU
(``CODE_REVIEW_CHUNK_OUTCOME_CACHE_SIZE``, via ``shared.cache``) keyed on the chunk's exact LLM input
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
coarser shared LRU (``CODE_REVIEW_SUBMISSION_CACHE_SIZE``, via ``shared.cache``)
keyed on the whole raw ``CodeReviewInput`` (files + task/spec/architecture
context + profile + resolved model) records the approved ``CodeReviewOutput`` of
each submission, and ``run_coordinator`` returns a freshly deserialized copy
before touching the LLM when the same submission comes back — zero LLM calls
(map, verification, and merge all skipped). Only approved outcomes are
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
import time
from typing import List, NamedTuple, Optional, Tuple

from llm_service import LLMClient, compact_text
from llm_service.interface import observer_turn_started_monotonic
from shared.cache import get_shared_cache
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
    split_block_into_segments,
)
from .false_positive_filter import CodebaseIndex, filter_false_positives
from .finding_combination import combine_findings, resolve_combine_similarity_threshold
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
    _normalized_severity,
    notify_review_progress,
)
from .profiles import ReviewProfile
from .repo_reader import RepoReader
from .side_effect_consolidation import MUTATION_ANALYSIS_ENV, SIDE_EFFECT_CONSOLIDATION_ENV
from .synthesis import synthesize_review_findings, synthesize_spec_compliance
from .transcript import model_label, record_transcript_entry

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

# Shared submission-level short-circuit cache (see module docstring's
# "Submission-level short-circuit" section). Bounded LRU mapping a
# whole-submission fingerprint -> the approved ``CodeReviewOutput`` it produced,
# so an identical, previously-approved submission returns without any LLM call.
# Backed by ``shared.cache``. ``0`` disables it (every run is a guaranteed miss).
# Coarser and independent of the per-chunk cache in ``mapping``.
DEFAULT_SUBMISSION_CACHE_SIZE = 256  # CODE_REVIEW_SUBMISSION_CACHE_SIZE, floor 0
# Base stem; ``_submission_cache_namespace()`` appends build id when configured.
_SUBMISSION_CACHE_NAMESPACE = "cr:sub:v1"


def _submission_cache_namespace() -> str:
    """Shared-cache namespace for submission short-circuit (includes build id)."""
    from shared.cache import with_cache_build_id  # noqa: PLC0415

    return with_cache_build_id(_SUBMISSION_CACHE_NAMESPACE)


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
        - This process's view of the shared submission cache namespace is empty
          when the call returns (best-effort across Redis). With no concurrent
          writers, the next review of any submission is a miss. Another worker
          may re-populate the same fingerprint between this clear and a later
          review, so a distributed miss is not absolutely guaranteed. Intended
          for tests and for callers that must force a cold review.
    """
    get_shared_cache(_submission_cache_namespace()).clear()


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


def _dedupe_issues(all_issues: List[CodeReviewIssue]) -> List[CodeReviewIssue]:
    """Dedupe issues by (file_path, line, description).

    Line is part of an issue's identity now that it anchors inline PR comments,
    so the same description on two different lines is two distinct findings,
    not a duplicate. An unanchored copy (line=None) of a finding that also
    appears anchored (same file_path+description) is dropped in favour of the
    anchored one, so the issue isn't reported twice (once in the body, once
    inline).

    Postconditions:
        - Order of first occurrence is preserved by walking ``all_issues`` in
          input order and appending to a list (membership uses a ``set``; order
          does not depend on set iteration).
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
                _normalized_severity(pair[1].severity), _CAP_UNKNOWN_SEVERITY_RANK
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
        - A reject with only non-critical/high issues, or with no actionable
          feedback at all, flips to approve. The merged summary is never consulted here:
          it mixes every chunk's text, so synthesizing a rejection from it
          could attribute an approving chunk's words to a rejecting chunk.
    """
    critical_or_high = [
        i for i in issues if _normalized_severity(i.severity) in ("critical", "high")
    ]
    approved = llm_approved and not critical_or_high
    if not approved and not critical_or_high:
        if issues:
            logger.info(
                "CodeReview: overriding to approved=True (%s non-critical/high issues, no critical/high)",
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
    """Run the additive pass, combine findings, then false-positive filter them.

    Three once-per-submission steps run **sequentially**, in dependency order,
    over the same shared ``CodebaseIndex`` (built once by the caller):

        1. The merged architecture-consistency + side-effect / blast-radius pass
           (a single LLM call returning the two finding lists separately).
        2. ``combine_findings`` over the full stream — the map-phase findings
           plus both additive lists — collapsing proximity/similarity
           near-duplicates (this subsumes the exact-match dedupe and the old
           side-effect consolidation).
        3. The false-positive filter over that combined set, so the additive
           findings are verified too (previously the filter ran concurrently
           with the merged pass and never saw its findings).

    Ordering matters here: the additive pass must precede combination and
    filtering so its findings are deduped and verified alongside the rest.

    Preconditions:
        - ``genuine_issues`` is the set of genuine chunk findings (coverage/
          safety findings excluded, per ``filter_false_positives``'s own
          precondition). It need not be pre-deduped: step 2 dedupes/combines.
        - ``shared_index`` was built from the same ``input_data``/``repo_reader``.

    Postconditions:
        - When ``input_data.skip_tail_passes`` is set, none of the three steps
          run (no LLM calls at all): returns a :class:`_TailPassResult` whose
          ``issues`` is ``genuine_issues`` unchanged and whose
          ``has_additive_findings`` is always False. This is a strict superset
          of ``skip_false_positive_filter``'s effect (setting both is redundant,
          not conflicting) — a lightweight mode for a fallback caller that wants
          speed over full tail-pass rigor.
        - Otherwise returns a :class:`_TailPassResult` whose ``issues`` is
          ``combine_findings([*genuine_issues, *architecture, *side_effect])``,
          then false-positive-filtered unless
          ``input_data.skip_false_positive_filter`` is set (which runs steps 1-2
          but skips the drop-false-positives step 3). ``has_additive_findings``
          is True iff either half of the merged pass contributed at least one
          finding (computed from the raw pass output, before combination).
        - Combination is fail-safe: any error in step 2 is logged and degrades
          to the uncombined stream rather than failing the review.
    """
    if input_data.skip_tail_passes:
        return _TailPassResult(issues=genuine_issues, has_additive_findings=False)

    # 1) Additive review pass FIRST: architecture-consistency + side-effect /
    #    mutation blast-radius, in a single merged LLM call. Running it before
    #    combination and false-positive filtering is what lets those findings
    #    join the main stream and be deduped/verified alongside the map findings
    #    (they previously ran concurrently with the FP filter and bypassed it).
    architecture_findings, side_effect_findings = find_architecture_and_side_effect_issues(
        llm, input_data, repo_reader=repo_reader, index=shared_index
    )
    combined = [*genuine_issues, *architecture_findings, *side_effect_findings]

    # 2) Combine near-duplicate / co-located findings across the WHOLE stream
    #    (proximity + same-file similarity). This subsumes both the exact-match
    #    ``_dedupe_issues`` for the main stream and the old side-effect
    #    consolidation, and shrinks the set the FP filter must verify. Fail-safe:
    #    any error degrades to the uncombined list rather than failing the review.
    try:
        combined = combine_findings(
            combined,
            shared_index,
            consolidate_side_effects=env_flag_enabled(SIDE_EFFECT_CONSOLIDATION_ENV),
        )
    except Exception:
        logger.exception(
            "CodeReviewCoordinator: finding combination failed; using uncombined tail-pass issues"
        )

    # 3) False-positive verification over the FULL combined set, so the additive
    #    architecture/side-effect findings are verified too. Skipped only when
    #    ``skip_false_positive_filter`` is set (a gate whose findings must never
    #    be silently dropped): that still runs the merged pass and combination.
    if not input_data.skip_false_positive_filter:
        combined = filter_false_positives(
            llm, input_data, combined, repo_reader=repo_reader, index=shared_index
        )

    return _TailPassResult(
        issues=combined,
        has_additive_findings=bool(architecture_findings) or bool(side_effect_findings),
    )


def _compact_for_review(
    text: str,
    max_chars: int,
    llm: LLMClient,
    content_description: str,
) -> str:
    """Compact shared review context and record each LLM call in the transcript.

    Preconditions:
        ``max_chars`` is a non-negative character budget.

    Postconditions:
        Returns ``compact_text(...)[:max_chars]``. Each ``llm.complete``
        compaction call is buffered as a ``compaction`` transcript entry
        (no-op when no ``job_id`` is bound). Cache hits make no LLM call and
        record nothing.
    """
    if max_chars < 0:
        raise ValueError("max_chars must be a non-negative character budget")
    last = time.monotonic()

    def _on_attempt(prompt: str, response: str) -> None:
        nonlocal last
        now = time.monotonic()
        started = observer_turn_started_monotonic()
        if started is None:
            started = last
        record_transcript_entry(
            "compaction",
            content_description,
            prompt,
            response,
            model=model_label(llm),
            duration_ms=(now - started) * 1000,
            started_monotonic=started,
        )
        last = now

    return compact_text(text, max_chars, llm, content_description, on_attempt=_on_attempt)[
        :max_chars
    ]


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
        - ``input_data`` carries the code under review via ``files``.
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
          touches the not-reviewed coverage findings. This pass runs after (not
          concurrently with) the merged architecture/side-effect pass and the
          finding-combination step: their sequential order, fail-safe behavior,
          and ``skip_tail_passes`` handling are exactly as documented on
          ``_run_tail_passes``, which this function calls unchanged.
        - Related ``side-effects`` findings are optionally consolidated as the
          ``side-effects`` special case of ``combine_findings`` (step 2 of
          ``_run_tail_passes``), gated by ``CODE_REVIEW_SIDE_EFFECT_CONSOLIDATION``
          and applied before the false-positive filter rather than as a separate
          post-filter step. The final deterministic dedupe below only folds in
          the coverage findings.
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
        - A submission byte-identical to one already approved *and fully
          reviewed* by any worker sharing the configured cache (Redis when
          ``REDIS_URL`` / ``REDIS_HOST`` is set, otherwise this process's
          in-memory LRU) — same code + context + model + output-affecting
          toggles including ``CODE_REVIEW_SIDE_EFFECT_CONSOLIDATION`` (folded
          in only for the ``CODE_REVIEW`` profile, the only profile it can
          affect), the combine-similarity threshold
          (``CODE_REVIEW_COMBINE_SIMILARITY_THRESHOLD``, folded in for every
          profile since it affects every profile's finding combination, not
          just ``CODE_REVIEW``'s side-effects), ``CODE_REVIEW_SPEC_COMPLIANCE_PASS``,
          and ``CODE_REVIEW_MUTATION_ANALYSIS``; no unreviewed ranges — returns
          the recorded approved output with no LLM call at all — unless a
          ``repo_reader`` is given, in which case this short-circuit never
          fires (a verdict that reads the rest of the repository cannot be
          safely reproduced from an input-only cache key; see module
          docstring's "Submission-level short-circuit" section). The
          cache-hit check and the deserialize of the served output go through
          ``shared.cache``; backend failures fail open to a miss / skipped
          write rather than raising into this review. The served object is a
          fresh deserialize, so callers may mutate it freely without
          corrupting the stored entry.
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

    # Same rationale as ``spec_compliance_single_pass`` above: the mutation-vs-
    # replaced-code contract sub-check (and the ``replaced_content`` before-image
    # it consumes) only ever runs under ``ReviewProfile.CODE_REVIEW`` -- both
    # ``side_effect_impact_pass`` and ``merged_architecture_side_effect_pass``
    # short-circuit to no findings on any other profile -- so a profile-blind
    # env read would fingerprint non-``CODE_REVIEW`` submissions as
    # flag-sensitive when the toggle can never actually affect their output,
    # causing needless cache misses whenever the env var happens to be set.
    mutation_analysis_enabled = env_flag_enabled(MUTATION_ANALYSIS_ENV) and (
        input_data.profile == ReviewProfile.CODE_REVIEW
    )

    # Same rationale again: ``combine_findings``'s ``consolidate_side_effects``
    # flag only changes output for ``side-effects``-category findings, and
    # those are only ever produced by ``find_architecture_and_side_effect_issues``
    # under ``ReviewProfile.CODE_REVIEW`` (it short-circuits to no findings on
    # every other profile) -- so a profile-blind env read would fingerprint
    # other profiles as flag-sensitive when the toggle can never affect them.
    side_effect_consolidation_enabled = env_flag_enabled(SIDE_EFFECT_CONSOLIDATION_ENV) and (
        input_data.profile == ReviewProfile.CODE_REVIEW
    )

    # Unlike the three toggles above, the combine-similarity threshold is NOT
    # profile-gated: ``combine_findings`` (via ``_run_tail_passes``) runs for
    # every profile and this threshold governs the generic proximity/same-anchor
    # merge rules for *every* finding category, not just ``side-effects`` --
    # so it is genuinely output-affecting for non-CODE_REVIEW submissions too.
    # Folding in a profile restriction here (unlike the others) would itself be
    # a bug: a threshold change could then silently fail to invalidate a cached
    # non-CODE_REVIEW verdict the new threshold should have altered.
    combine_similarity_threshold = resolve_combine_similarity_threshold()

    # Submission-level short-circuit (see module docstring's "Submission-level
    # short-circuit" section). An identical approved submission returns its
    # cached output before any LLM work. Keyed on the raw input + model +
    # output-affecting toggles — no compaction — so the check itself costs no
    # model call. Skipped entirely when disabled (size 0) or when a
    # ``repo_reader`` is given. On a miss the run proceeds and stores its
    # verdict below if approved.
    submission_capacity = _submission_cache_size()
    submission_key: Optional[str] = None
    cached: Optional[CodeReviewOutput] = None
    if submission_capacity > 0 and repo_reader is None:
        submission_key = _submission_fingerprint(
            input_data,
            model_fingerprint,
            spec_compliance_single_pass,
            mutation_analysis_enabled,
            side_effect_consolidation_enabled,
            combine_similarity_threshold,
        )
        cache = get_shared_cache(_submission_cache_namespace())
        # shared.cache is fail-open, but keep an explicit local guard so a
        # misbehaving backend / unexpected raise never aborts the review.
        try:
            raw = cache.get(submission_key)
        except Exception:
            logger.warning(
                "CodeReviewCoordinator: submission cache get failed; treating as miss",
                exc_info=True,
            )
            raw = None
        if raw is not None:
            # Fresh deserialize — independent of the stored entry (same guarantee
            # as the former under-lock model_copy). Unreadable / schema-skewed
            # Redis entries fail open to a miss so a deploy never aborts a review.
            try:
                cached = CodeReviewOutput.model_validate_json(raw)
            except Exception:
                logger.warning(
                    "CodeReviewCoordinator: corrupt submission cache entry for %s; treating as miss",
                    submission_key,
                    exc_info=True,
                )
                try:
                    cache.delete(submission_key)
                except Exception:
                    logger.warning(
                        "CodeReviewCoordinator: submission cache delete failed after corrupt entry",
                        exc_info=True,
                    )
                cached = None
        if cached is not None:
            # Only fully-clean approved verdicts are eligible: a hit with
            # ``not_reviewed_ranges`` would skip re-review of ranges that were
            # previously degraded / unreviewed (same gate as the write path).
            if not cached.approved or cached.not_reviewed_ranges:
                logger.warning(
                    "CodeReviewCoordinator: cached submission %s is not a clean "
                    "approval (approved=%s, not_reviewed_ranges=%s); treating as miss",
                    submission_key,
                    cached.approved,
                    cached.not_reviewed_ranges,
                )
                try:
                    cache.delete(submission_key)
                except Exception:
                    logger.warning(
                        "CodeReviewCoordinator: submission cache delete failed for unclean entry",
                        exc_info=True,
                    )
                cached = None
            else:
                logger.info(
                    "CodeReviewCoordinator: submission cache hit; skipping review (approved)"
                )
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
    spec_content = _compact_for_review(
        input_data.spec_content or "", max_spec, llm, "specification"
    )
    arch_overview = ""
    if input_data.architecture:
        arch_overview = _compact_for_review(
            _render_architecture_context(input_data.architecture),
            max_arch,
            llm,
            "architecture overview",
        )
    existing_codebase = _compact_for_review(
        input_data.existing_codebase or "", max_existing, llm, "existing codebase"
    )

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

    # Genuine chunk findings handed to the tail passes. Coverage/safety findings
    # (``not_reviewed_issues``, empty-file notices) are never passed in, so the
    # gate's anti-loop nets stay intact. No exact dedupe here: ``_run_tail_passes``
    # now runs ``combine_findings`` over the full stream (map + additive), which
    # subsumes both the exact-match dedupe and the side-effect consolidation
    # before the false-positive filter runs.
    genuine_issues = list(outcome.issues)
    notify_review_progress(
        progress_callback,
        "verifying",
        f"verifying {len(genuine_issues)} findings against the full codebase",
        _PROGRESS_VERIFYING,
    )
    # Built once and shared with the merged architecture/side-effect pass, the
    # finding-combination step, and the false-positive filter: all read the same
    # submission/repo_reader, so a single index avoids parsing the submission
    # twice. CodebaseIndex is read-only after construction (see its own
    # docstring's Invariants), so this one instance is safe to hand to the tail
    # passes (see ``_run_tail_passes``). It is a local: passed only into the tail
    # passes and never stored on any coordinator/instance state, so it is not
    # retained past this call and cannot outlive the submission it describes.
    shared_index = CodebaseIndex.from_input(input_data, repo_reader=repo_reader)

    # ``_run_tail_passes`` now runs sequentially: the merged architecture/
    # side-effect pass, then ``combine_findings`` over the full stream (which
    # subsumes the exact-match dedupe and side-effect consolidation), then the
    # false-positive filter over everything (so additive findings are verified
    # too). ``skip_false_positive_filter`` is for a gate whose findings must
    # never be silently dropped; it removes only the drop-false-positives step,
    # so it can only ever keep more findings. The merged halves are restricted
    # internally to the default CODE_REVIEW profile -- see their own docstrings
    # for why the other profiles must never receive these findings.
    tail_pass_result = _run_tail_passes(
        llm=llm,
        input_data=input_data,
        genuine_issues=genuine_issues,
        repo_reader=repo_reader,
        shared_index=shared_index,
    )
    tail_pass_issues = tail_pass_result.issues

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
    # module docstring). A rejection is not stored — the fix that follows changes
    # the submission, and if the same rejected bytes reappear the (mostly
    # cached) map phase still surfaces the findings the coding agent needs. A
    # run that left any range unreviewed is also not stored: freezing it would
    # keep serving a partial verdict on later identical cycles instead of
    # re-attempting the chunk that could not be reviewed (a
    # semantic-exhaustion/truncation hiccup may not recur), matching the
    # map-phase rule that degraded chunk outcomes are never cached. Serialize
    # directly to opaque bytes — the cache stores an immutable payload, so a
    # deep clone of the Pydantic object is unnecessary.
    if submission_key is not None and result.approved and len(not_reviewed_ranges) == 0:
        payload = result.model_dump_json().encode("utf-8")
        try:
            get_shared_cache(_submission_cache_namespace()).set(
                submission_key,
                payload,
                max_entries=submission_capacity,
            )
        except Exception:
            logger.warning(
                "CodeReviewCoordinator: submission cache set failed; continuing without cache write",
                exc_info=True,
            )
        else:
            logger.info(
                "CodeReviewCoordinator: cached approved submission under key=%s (bytes=%d)",
                submission_key,
                len(payload),
            )
    return result
