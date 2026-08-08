"""Snapshot-comparison harness: two-call vs merged architecture/side-effect pass.

Standalone dev tool (not part of the coordinator/Temporal request path) that
runs a fixed corpus of real historical submissions from this repository
through both the pre-consolidation two-call path
(``find_architecture_and_redundancy_issues`` + ``find_side_effect_impact_issues``)
and the post-consolidation one-call path
(``find_architecture_and_side_effect_issues``), then diffs the resulting
findings so a human can judge whether the merge lost or added findings.

This module makes NO claim about whether the merge is safe — it only produces
the comparison data. See ``docs/snapshot-comparison-report.md`` for the
corpus rationale, how to run this for real, and the (pending) go/no-go
recommendation.

Invariants:

    - **Read-only.** Never mutates the caller's repository; each submission is
      materialized into a throwaway ``git worktree`` that is always removed,
      even on failure.
    - **Additive-pass parity.** Calls the exact same public entry points the
      coordinator and Temporal activities use — no reimplementation of pass
      logic, parsing, or validation.
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from shared.dev_models.models import SystemArchitecture
from shared.env import env_flag_enabled
from shared.git.git_utils import add_worktree, remove_worktree

from .architecture_consistency_pass import find_architecture_and_redundancy_issues
from .false_positive_filter import CodebaseIndex
from .merged_architecture_side_effect_pass import find_architecture_and_side_effect_issues
from .models import CodeReviewInput, CodeReviewIssue
from .repo_reader import DiskRepoReader, RepoReader, disk_repo_reader_from_root
from .side_effect_impact_pass import find_side_effect_impact_issues

logger = logging.getLogger(__name__)

# Text-similarity threshold above which a lost/added pair is considered the
# "same" finding reworded rather than two genuinely different findings.
# Independent calls (even the two-call path's own two runs) never reproduce a
# finding's wording verbatim, so an exact-string match would misreport almost
# every real match as a lost+added pair.
_MATCH_TEXT_THRESHOLD = 0.45

# A cited line more than this far from the other side's citation is treated as
# a different location even when the text is similar (e.g. two distinct
# docstring-drift findings on the same file).
_MATCH_LINE_TOLERANCE = 5

# Same env vars the standalone/merged passes gate on (architecture_consistency_pass.py,
# side_effect_impact_pass.py). When either is explicitly disabled, the affected pass
# returns [] on BOTH the old and new call paths without making any LLM call -- see
# _require_passes_enabled.
_ARCH_PASS_ENV = "CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS"
_SIDE_PASS_ENV = "CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS"


@dataclass(frozen=True)
class SubmissionSpec:
    """One corpus entry: a real historical commit plus which files to review.

    ``commit_sha`` must be reachable in the repository this harness runs
    against (a squash-merge commit on the default branch, not a feature
    branch's now-deleted head — see ``docs/snapshot-comparison-report.md``
    for how the corpus's SHAs were located). ``label`` is the sole
    human-readable identifier for a corpus entry (used in output and in the
    worktree directory name), so it must be unique within ``CORPUS``.
    """

    label: str
    commit_sha: str
    changed_files: Tuple[str, ...]
    task_description: str
    with_architecture_doc: bool = False


# Representative sample of real merged changes in this repository, spanning
# finding density (docstring-only -> 9-file feature change) and the
# with/without architecture-document axis the merged pass's design doc calls
# out as a risk. See docs/snapshot-comparison-report.md for the selection
# rationale and how each commit_sha was located.
CORPUS: Tuple[SubmissionSpec, ...] = (
    SubmissionSpec(
        label="clean-baseline (docstring-only)",
        commit_sha="9036ef9f3f8027881ea2a759886e5f4708e8b308",
        changed_files=("backend/agents/software_engineering_team/code_review_agent/chunking.py",),
        task_description="Fix misleading _clean_str docstring summary",
    ),
    SubmissionSpec(
        label="side-effect (small cross-file threading)",
        commit_sha="83299e45c6e5e94140808fb43155fd54cc1c9e43",
        changed_files=("backend/agents/software_engineering_team/temporal/workflows.py",),
        task_description="Thread sprint_id through RunTeamWorkflow",
    ),
    SubmissionSpec(
        label="architecture/refactor (dead-code removal)",
        commit_sha="c8b409856c07e765d9d4b79717f25b37d1f609e8",
        changed_files=(
            "backend/agents/software_engineering_team/shared/production_review_agents.py",
        ),
        task_description="Delete thread-mode variant of build_production_review_kwargs",
    ),
    SubmissionSpec(
        label="architecture + side-effect (Temporal wiring, no doc)",
        commit_sha="569b78ebc785d1efd1098685c2f0dda4cf669263",
        changed_files=(
            "backend/agents/software_engineering_team/code_review_agent/temporal/__init__.py",
            "backend/agents/software_engineering_team/code_review_agent/temporal/activities.py",
            "backend/agents/software_engineering_team/code_review_agent/temporal/workflows.py",
        ),
        task_description="Merge architecture-consistency and side-effect-impact passes "
        "in the Temporal code review workflow",
        with_architecture_doc=False,
    ),
    SubmissionSpec(
        label="large multi-file feature, with architecture doc",
        commit_sha="358873b64294e74f14d029d96e31479284601403",
        changed_files=(
            "backend/agents/software_engineering_team/code_review_agent/__init__.py",
            "backend/agents/software_engineering_team/code_review_agent/architecture_consistency_pass.py",
            "backend/agents/software_engineering_team/code_review_agent/architecture_context.py",
            "backend/agents/software_engineering_team/code_review_agent/coordinator.py",
            "backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py",
            "backend/agents/software_engineering_team/code_review_agent/merged_architecture_side_effect_pass.py",
            "backend/agents/software_engineering_team/code_review_agent/models.py",
            "backend/agents/software_engineering_team/code_review_agent/prompts.py",
            "backend/agents/software_engineering_team/code_review_agent/side_effect_impact_pass.py",
            "backend/agents/software_engineering_team/shared/context_sizing.py",
        ),
        task_description="Implement merged architecture/side-effect pass in the "
        "in-process coordinator",
        with_architecture_doc=True,
    ),
    SubmissionSpec(
        label="large multi-file feature, without architecture doc",
        commit_sha="358873b64294e74f14d029d96e31479284601403",
        changed_files=(
            "backend/agents/software_engineering_team/code_review_agent/__init__.py",
            "backend/agents/software_engineering_team/code_review_agent/architecture_consistency_pass.py",
            "backend/agents/software_engineering_team/code_review_agent/architecture_context.py",
            "backend/agents/software_engineering_team/code_review_agent/coordinator.py",
            "backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py",
            "backend/agents/software_engineering_team/code_review_agent/merged_architecture_side_effect_pass.py",
            "backend/agents/software_engineering_team/code_review_agent/models.py",
            "backend/agents/software_engineering_team/code_review_agent/prompts.py",
            "backend/agents/software_engineering_team/code_review_agent/side_effect_impact_pass.py",
            "backend/agents/software_engineering_team/shared/context_sizing.py",
        ),
        task_description="Implement merged architecture/side-effect pass in the "
        "in-process coordinator",
        with_architecture_doc=False,
    ),
)

_ARCHITECTURE_DOC_PATH = "docs/ARCHITECTURE.md"


class SnapshotComparisonError(RuntimeError):
    """Raised when a corpus submission cannot be materialized or reviewed."""


def _require_passes_enabled() -> None:
    """Fail fast when either pass is disabled, rather than silently comparing nothing.

    Both the standalone passes and the merged pass early-return ``[]`` with NO
    LLM call at all when their own env flag is off (see
    ``architecture_consistency_pass.find_architecture_and_redundancy_issues`` /
    ``side_effect_impact_pass.find_side_effect_impact_issues``). If that flag
    happens to be off in whatever environment a real comparison run uses, the
    affected category would show "0 lost, 0 added" on every submission — a
    clean-looking report for a category that was never actually exercised on
    either path, silently invalidating the comparison.

    Postconditions: raises :class:`SnapshotComparisonError` naming every
        disabled flag when :data:`_ARCH_PASS_ENV` or :data:`_SIDE_PASS_ENV`
        (checked via the same ``env_flag_enabled`` the passes themselves use)
        is disabled; returns normally when both are enabled (the default).
    """
    disabled = [name for name in (_ARCH_PASS_ENV, _SIDE_PASS_ENV) if not env_flag_enabled(name)]
    if disabled:
        raise SnapshotComparisonError(
            f"{', '.join(disabled)} is disabled in this environment -- the affected "
            "pass(es) would never actually run (no LLM call, [] on both paths for every "
            "submission), producing a misleadingly clean comparison instead of a real one. "
            "Enable both env flags before running a snapshot comparison."
        )


def materialize_submission(
    spec: SubmissionSpec, repo_path: Path, worktree_root: Path
) -> Tuple[CodeReviewInput, Optional[DiskRepoReader], Path]:
    """Check out ``spec.commit_sha`` and build its ``CodeReviewInput``.

    Preconditions:
        - ``repo_path`` is an existing git checkout that has ``spec.commit_sha``
          reachable (a shallow clone must have fetched enough history).
        - ``worktree_root`` is a directory this call may create subdirectories
          under.

    Postconditions:
        - Returns ``(input_data, repo_reader, worktree_path)`` where
          ``input_data.files`` holds the checked-out content of
          ``spec.changed_files`` (missing/unreadable files are skipped, never
          raising for one bad path — the rest of the submission still reviews),
          ``repo_reader`` is a ``DiskRepoReader`` rooted at the worktree (so
          off-diff reads see the real rest-of-repo state at that commit), and
          ``worktree_path`` is the caller's responsibility to remove via
          :func:`cleanup_worktree`.
        - ``input_data.architecture`` is populated from
          ``docs/ARCHITECTURE.md`` in the worktree when
          ``spec.with_architecture_doc`` is True and that file exists, else
          ``None``.
    Raises:
        - :class:`SnapshotComparisonError` when the worktree itself cannot be
          created (bad SHA, not enough history, etc.) or no changed file could
          be read.
    """
    worktree_path = worktree_root / f"{spec.commit_sha[:12]}-{id(spec)}"
    ok, msg = add_worktree(repo_path, worktree_path, ref=spec.commit_sha)
    if not ok:
        raise SnapshotComparisonError(
            f"Could not check out {spec.commit_sha} ({spec.label!r}): {msg}"
        )

    files: Dict[str, str] = {}
    for rel_path in spec.changed_files:
        full_path = worktree_path / rel_path
        try:
            files[rel_path] = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Could not read %s from %s: %s", rel_path, worktree_path, exc)
    if not files:
        remove_worktree(repo_path, worktree_path, force=True)
        raise SnapshotComparisonError(
            f"None of {spec.label!r}'s changed files could be read from {worktree_path}"
        )

    architecture: Optional[SystemArchitecture] = None
    if spec.with_architecture_doc:
        doc_path = worktree_path / _ARCHITECTURE_DOC_PATH
        try:
            doc_text = doc_path.read_text(encoding="utf-8", errors="replace")
            architecture = SystemArchitecture(
                overview=f"See {_ARCHITECTURE_DOC_PATH} (Khala platform architecture).",
                architecture_document=doc_text,
            )
        except OSError as exc:
            logger.warning("with_architecture_doc set but %s unreadable: %s", doc_path, exc)

    repo_reader = disk_repo_reader_from_root(str(worktree_path))
    input_data = CodeReviewInput(
        files=files,
        task_description=spec.task_description,
        architecture=architecture,
        repo_root=str(worktree_path),
    )
    return input_data, repo_reader, worktree_path


def cleanup_worktree(repo_path: Path, worktree_path: Path) -> None:
    """Best-effort remove a worktree created by :func:`materialize_submission`.

    Postconditions: never raises; a removal failure is logged, matching every
        other worktree cleanup path in this codebase (see ``WorktreeManager.cleanup``).
    """
    ok, msg = remove_worktree(repo_path, worktree_path, force=True)
    if not ok:
        logger.warning("Failed to remove worktree %s: %s", worktree_path, msg)


class _FailureCapturingHandler(logging.Handler):
    """Detects whether a pass logged its documented fail-safe warning.

    Each pass (``architecture_consistency_pass``, ``side_effect_impact_pass``,
    ``merged_architecture_side_effect_pass``) never raises to its caller by
    design — a setup/LLM/parse failure is caught internally and degrades to
    an empty finding list, logged at WARNING via that module's own
    ``logging.getLogger(__name__)`` (a documented, stable contract, not a
    private implementation detail). That empty list is indistinguishable
    from a genuine "found nothing" result to a caller that only looks at the
    return value — this harness needs the distinction (see
    :func:`_call_pass_detecting_failure`), so it listens for that warning
    instead of reaching into the pass's private internals to bypass its
    fail-safe try/except.

    Known blind spot: a pass only logs this warning on a hard failure
    (exception, provider error) — a provider reply that is valid JSON but
    missing/wrong-typed findings array degrades to ``[]`` with no warning at
    all, and this handler cannot tell that apart from a genuine empty
    result. Catching it would mean inspecting the raw completion before the
    pass's own parsing, i.e. intercepting calls inside the ``strands.Agent``
    tool-calling loop each pass runs (tool-use turns interleaved with the
    final findings turn) — this harness has no reliable way to tell those
    apart from outside that loop, so it does not attempt it (see
    ``docs/snapshot-comparison-report.md``'s Methodology section for the
    resulting review guidance).
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.failed = False

    def emit(self, record: logging.LogRecord) -> None:
        self.failed = True


def _call_pass_detecting_failure(fn: Callable[[], object], logger_name: str) -> Tuple[object, bool]:
    """Call ``fn()``, reporting whether ``logger_name`` logged a WARNING during the call.

    Preconditions:
        - ``logger_name`` is the ``__name__``-derived logger of the pass module
          ``fn`` invokes. Callers should pass the invoked function's own
          ``__module__`` attribute (not a hard-coded string) so the name
          matches however this package was actually imported — bare
          (``code_review_agent.architecture_consistency_pass``, under
          pytest's path insertion) or fully qualified
          (``software_engineering_team.code_review_agent.architecture_consistency_pass``).

    Postconditions:
        - Returns ``(fn()'s result, failed)`` where ``failed`` is True iff that
          logger emitted a WARNING (or higher) during the call — the pass's own
          fail-safe failure log line, per this module's own docstring
          guarantee ("any setup or LLM failure is logged at warning level").
          The temporary handler is always removed, even if ``fn`` raises
          (it shouldn't, per that same fail-safe contract, but this must not
          leak a handler if it ever does).
    """
    handler = _FailureCapturingHandler()
    target_logger = logging.getLogger(logger_name)
    target_logger.addHandler(handler)
    try:
        result = fn()
    finally:
        target_logger.removeHandler(handler)
    return result, handler.failed


def run_two_call(
    llm: object,
    input_data: CodeReviewInput,
    repo_reader: Optional[RepoReader],
    index: CodebaseIndex,
) -> Tuple[List[CodeReviewIssue], List[CodeReviewIssue], bool, bool]:
    """Run the pre-consolidation path: two independent per-submission calls.

    Postconditions: returns ``(architecture_findings, side_effect_findings,
        architecture_failed, side_effect_failed)`` — finding lists identical
        in shape to :func:`run_merged_call`'s return, calling the standalone
        passes exactly as ``coordinator._run_tail_passes`` did before the
        merge (recoverable verbatim from commit ``358873b^``); the two
        ``_failed`` flags come from :func:`_call_pass_detecting_failure` and
        are independent (either, both, or neither may be True).
    """
    architecture, architecture_failed = _call_pass_detecting_failure(
        lambda: find_architecture_and_redundancy_issues(
            llm, input_data, repo_reader=repo_reader, index=index
        ),
        find_architecture_and_redundancy_issues.__module__,
    )
    side_effect, side_effect_failed = _call_pass_detecting_failure(
        lambda: find_side_effect_impact_issues(
            llm, input_data, repo_reader=repo_reader, index=index
        ),
        find_side_effect_impact_issues.__module__,
    )
    return architecture, side_effect, architecture_failed, side_effect_failed


def run_merged_call(
    llm: object,
    input_data: CodeReviewInput,
    repo_reader: Optional[RepoReader],
    index: CodebaseIndex,
) -> Tuple[List[CodeReviewIssue], List[CodeReviewIssue], bool]:
    """Run the post-consolidation path: one merged per-submission call.

    Postconditions: returns ``(architecture_findings, side_effect_findings,
        failed)`` — finding lists exactly as
        ``find_architecture_and_side_effect_issues`` returns them; ``failed``
        (from :func:`_call_pass_detecting_failure`) covers BOTH halves, since
        one LLM call produces both.
    """
    (architecture, side_effect), failed = _call_pass_detecting_failure(
        lambda: find_architecture_and_side_effect_issues(
            llm, input_data, repo_reader=repo_reader, index=index
        ),
        find_architecture_and_side_effect_issues.__module__,
    )
    return architecture, side_effect, failed


@dataclass
class FindingDiff:
    """Heuristic pairing of two finding lists for the same category axis.

    ``matched`` pairs are NOT guaranteed to be the same underlying finding —
    independent LLM calls never reproduce identical wording — this is a
    similarity heuristic to shrink the human-review surface to ``lost``/
    ``added`` only, not an automated regression verdict.
    """

    matched: List[Tuple[CodeReviewIssue, CodeReviewIssue]] = field(default_factory=list)
    lost: List[CodeReviewIssue] = field(default_factory=list)
    added: List[CodeReviewIssue] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "matched": [{"old": o.model_dump(), "new": n.model_dump()} for o, n in self.matched],
            "lost": [i.model_dump() for i in self.lost],
            "added": [i.model_dump() for i in self.added],
        }


def _finding_similarity(a: CodeReviewIssue, b: CodeReviewIssue) -> float:
    """Score how likely ``a``/``b`` describe the same underlying finding.

    Postconditions: returns 0.0 when categories differ, both cite a
        non-blank, different ``file_path``, or both cite a ``line`` more than
        ``_MATCH_LINE_TOLERANCE`` apart (a hard reject, not a penalty: two
        findings with identical or near-identical wording at genuinely
        different locations in the same file — e.g. the same lint-style
        issue on two different functions — are different findings, and a
        mere score reduction is not enough to keep them from clearing
        ``_MATCH_TEXT_THRESHOLD`` when their text similarity is high).
        Otherwise returns the ``difflib.SequenceMatcher`` ratio over
        ``description``. Pure; never raises.
    """
    if a.category != b.category:
        return 0.0
    if a.file_path and b.file_path and a.file_path != b.file_path:
        return 0.0
    if a.line is not None and b.line is not None and abs(a.line - b.line) > _MATCH_LINE_TOLERANCE:
        return 0.0
    return difflib.SequenceMatcher(None, a.description.lower(), b.description.lower()).ratio()


@dataclass
class _DedupeGroup:
    """One cross-repeat cluster :func:`_dedupe_pooled` folded into one representative.

    ``collapsed`` is kept for audit, not discarded: a text-similarity
    heuristic run over pooled findings with no other signal CANNOT reliably
    tell "the same real finding recurring across repeats" apart from "two
    different findings that happen to be worded similarly and land in
    different repeats" (e.g. a read-path violation in one repeat, a
    similarly-worded write-path violation in another) — that ambiguity is
    irreducible from pooled text alone, so the collapsed originals are
    surfaced in the report for a human to re-judge, rather than the harness
    silently guessing one way or the other. See
    ``docs/snapshot-comparison-report.md``.
    """

    representative: CodeReviewIssue
    collapsed: List[CodeReviewIssue] = field(default_factory=list)


def _dedupe_pooled(runs: Sequence[Sequence[CodeReviewIssue]]) -> List[_DedupeGroup]:
    """Collapse cross-repeat duplicate findings for one path, one repeat at a time.

    Preconditions:
        - ``runs`` holds one inner sequence per repeat, each the findings that
          SINGLE repeat emitted for one path and one category axis (repeat
          provenance must be preserved by the caller — flattening repeats
          into one list before calling this loses the information this
          function needs).

    Postconditions:
        - Returns one :class:`_DedupeGroup` per group of cross-repeat
          duplicates, in first-seen order. A finding is folded into an
          existing group's ``collapsed`` only when it is the BEST-scoring
          still-available match (``_finding_similarity(...) >=
          _MATCH_TEXT_THRESHOLD``, mirroring :func:`diff_findings`'s greedy
          matcher) among groups whose representative came from a STRICTLY
          EARLIER repeat — two findings emitted by the SAME repeat are never
          compared against each other, so two genuinely distinct findings a
          single run co-emits always start their own groups, never
          collapsing into one. A pre-existing group can absorb AT MOST ONE
          finding per later repeat (it is removed from consideration for the
          rest of that repeat's findings once claimed): without this, a
          later repeat that emits both a real recurrence AND an unrelated
          finding that both happen to resemble the same earlier
          representative would incorrectly collapse both into it, discarding
          the unrelated one instead of giving it its own group. Without
          cross-repeat dedup at all, a finding one path's repeats emit
          inconsistently (e.g. 1 of 3 runs) would pool to fewer
          representatives than the other path's consistent emission (e.g. 3
          of 3 runs), and the 1:1 cross-path matcher in :func:`diff_findings`
          would then report the surplus as spurious ``lost``/``added`` noise
          instead of one real match. Pure; never raises.
    """
    groups: List[_DedupeGroup] = []
    for run in runs:
        available = list(groups)  # pre-existing groups only; consumed at most once each this run
        for finding in run:
            best_index: Optional[int] = None
            best_score = 0.0
            for i, group in enumerate(available):
                score = _finding_similarity(finding, group.representative)
                if score > best_score:
                    best_score = score
                    best_index = i
            if best_index is not None and best_score >= _MATCH_TEXT_THRESHOLD:
                available.pop(best_index).collapsed.append(finding)
            else:
                groups.append(_DedupeGroup(representative=finding))
    return groups


def _collapse_report(groups: Sequence[_DedupeGroup], *, path: str) -> List[dict]:
    """JSON-friendly audit trail of every group that actually collapsed >1 finding.

    Preconditions:
        - ``path`` identifies which call path ``groups`` came from (``"old"``
          or ``"new"``) — a reviewer auditing a collapse needs to know which
          side it happened on (a wrongly-collapsed old-path finding is a
          candidate regression; a wrongly-collapsed new-path finding is a
          candidate addition), which is unrecoverable once old/new reports
          are combined without this label.

    Postconditions: returns one
        ``{"path": ..., "representative": ..., "collapsed": [...]}`` dict per
        group with a non-empty ``collapsed`` list; groups that never matched
        a later repeat's finding are omitted (nothing to audit). Pure; never
        raises.
    """
    return [
        {
            "path": path,
            "representative": g.representative.model_dump(),
            "collapsed": [f.model_dump() for f in g.collapsed],
        }
        for g in groups
        if g.collapsed
    ]


def diff_findings(old: Sequence[CodeReviewIssue], new: Sequence[CodeReviewIssue]) -> FindingDiff:
    """Greedily pair ``old``/``new`` findings by similarity within one category axis.

    Preconditions:
        - ``old``/``new`` are both from the SAME category axis (architecture-only
          or side-effect-only) — mixing axes would let an architecture finding
          spuriously "match" a side-effect finding on shared wording.

    Postconditions:
        - Each ``old`` finding is greedily paired with its best-scoring unused
          ``new`` finding scoring ``>= _MATCH_TEXT_THRESHOLD``
          (:func:`_finding_similarity`); unpaired ``old`` findings are
          ``lost``, unpaired ``new`` findings are ``added``. Every input
          finding appears in exactly one of ``matched``/``lost``/``added``.
          Pure; never raises.
    """
    remaining_new = list(new)
    matched: List[Tuple[CodeReviewIssue, CodeReviewIssue]] = []
    lost: List[CodeReviewIssue] = []
    for o in old:
        best_index: Optional[int] = None
        best_score = 0.0
        for i, n in enumerate(remaining_new):
            score = _finding_similarity(o, n)
            if score > best_score:
                best_score = score
                best_index = i
        if best_index is not None and best_score >= _MATCH_TEXT_THRESHOLD:
            matched.append((o, remaining_new.pop(best_index)))
        else:
            lost.append(o)
    return FindingDiff(matched=matched, lost=lost, added=remaining_new)


@dataclass
class SubmissionComparisonResult:
    label: str
    architecture_diff: FindingDiff
    side_effect_diff: FindingDiff
    old_llm_calls: int
    new_llm_calls: int
    architecture_cross_repeat_collapses: List[dict] = field(default_factory=list)
    side_effect_cross_repeat_collapses: List[dict] = field(default_factory=list)
    old_call_failures: int = 0
    new_call_failures: int = 0

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "old_llm_calls": self.old_llm_calls,
            "new_llm_calls": self.new_llm_calls,
            "old_call_failures": self.old_call_failures,
            "new_call_failures": self.new_call_failures,
            "architecture": self.architecture_diff.to_dict(),
            "side_effect": self.side_effect_diff.to_dict(),
            "architecture_cross_repeat_collapses": self.architecture_cross_repeat_collapses,
            "side_effect_cross_repeat_collapses": self.side_effect_cross_repeat_collapses,
        }


def compare_submission(
    spec: SubmissionSpec,
    llm_factory: Callable[[], object],
    repo_path: Path,
    worktree_root: Path,
    repeats: int = 1,
) -> SubmissionComparisonResult:
    """Materialize one corpus entry and diff its old-path vs merged-path findings.

    Preconditions:
        - ``repeats >= 1``. ``llm_factory()`` returns a fresh, ready-to-use LLM
          client each call (a real provider client has no temperature control
          exposed to this harness, so repeating is the only lever against
          sampling variance — see ``docs/snapshot-comparison-report.md``).

    Postconditions:
        - Returns a result whose ``architecture_diff``/``side_effect_diff``
          are computed over each path's ``repeats`` runs, deduplicated via
          :func:`_dedupe_pooled` (which preserves repeat provenance, so two
          distinct findings co-emitted by the SAME repeat are never collapsed
          into each other) before the cross-path diff — so a finding one path
          emits in every repeat and the other emits in only one still counts
          as a single match, not surplus ``lost``/``added`` noise from the
          unequal repeat counts. Every cross-repeat collapse the dedup step
          actually made (on either path) is recorded in
          ``architecture_cross_repeat_collapses``/``side_effect_cross_repeat_collapses``
          for audit — the similarity heuristic cannot distinguish a true
          repeated finding from two distinct findings that happen to land in
          different repeats with similar wording, so that judgment call is
          never silent.
        - ``old_call_failures``/``new_call_failures`` count how many of that
          path's pass calls internally caught a setup/LLM/parse failure and
          fail-safe-degraded to an empty finding list (see
          :func:`_call_pass_detecting_failure`) — a failed call and a
          genuine "found nothing" call both return ``[]`` to this function,
          so a caller that ignores these counts cannot tell them apart. A
          non-zero count means this submission's diff was computed from
          fewer real samples than ``repeats`` implies and should be weighted
          accordingly, not read as a confident regression/false-positive
          result.
        - Which path runs first alternates by repeat index (old-then-new on
          even repeats, new-then-old on odd repeats), so a rate-limited or
          time-degrading provider does not systematically disadvantage
          whichever path always ran second within a repeat — an ordering
          bias that repeating with a fixed order would amplify, not average
          out. This alternation cannot exactly balance an ODD ``repeats``
          count (one path is unavoidably first one more time than the
          other — see :func:`_warn_if_repeats_imbalanced`); pass an even
          ``repeats`` for a fully counterbalanced real run.
        - The worktree is always removed, even when a pass call raises.

    Raises:
        - :class:`SnapshotComparisonError` when either pass is disabled via
          its env flag (see :func:`_require_passes_enabled`) — comparing
          with a disabled pass would silently report a clean "0 lost, 0
          added" result for a category neither path actually exercised.
    """
    assert repeats >= 1, "repeats must be >= 1"
    _require_passes_enabled()
    _warn_if_repeats_imbalanced(repeats)
    input_data, repo_reader, worktree_path = materialize_submission(spec, repo_path, worktree_root)
    try:
        index = CodebaseIndex.from_input(input_data, repo_reader=repo_reader)
        old_arch_runs: List[List[CodeReviewIssue]] = []
        old_side_runs: List[List[CodeReviewIssue]] = []
        new_arch_runs: List[List[CodeReviewIssue]] = []
        new_side_runs: List[List[CodeReviewIssue]] = []
        old_call_failures = 0
        new_call_failures = 0
        for i in range(repeats):
            steps = ["old", "new"] if i % 2 == 0 else ["new", "old"]
            for step in steps:
                if step == "old":
                    a, s, arch_failed, side_failed = run_two_call(
                        llm_factory(), input_data, repo_reader, index
                    )
                    old_arch_runs.append(a)
                    old_side_runs.append(s)
                    old_call_failures += int(arch_failed) + int(side_failed)
                else:
                    a2, s2, merged_failed = run_merged_call(
                        llm_factory(), input_data, repo_reader, index
                    )
                    new_arch_runs.append(a2)
                    new_side_runs.append(s2)
                    new_call_failures += int(merged_failed)
        if old_call_failures or new_call_failures:
            logger.warning(
                "compare_submission(%r): %s old-path call failure(s), %s new-path call "
                "failure(s) fail-safe-degraded to empty findings -- treat this "
                "submission's diff as computed from fewer real samples than repeats=%s "
                "implies.",
                spec.label,
                old_call_failures,
                new_call_failures,
                repeats,
            )
        old_arch_groups = _dedupe_pooled(old_arch_runs)
        new_arch_groups = _dedupe_pooled(new_arch_runs)
        old_side_groups = _dedupe_pooled(old_side_runs)
        new_side_groups = _dedupe_pooled(new_side_runs)
        return SubmissionComparisonResult(
            label=spec.label,
            architecture_diff=diff_findings(
                [g.representative for g in old_arch_groups],
                [g.representative for g in new_arch_groups],
            ),
            side_effect_diff=diff_findings(
                [g.representative for g in old_side_groups],
                [g.representative for g in new_side_groups],
            ),
            old_llm_calls=repeats * 2,
            new_llm_calls=repeats * 1,
            old_call_failures=old_call_failures,
            new_call_failures=new_call_failures,
            architecture_cross_repeat_collapses=(
                _collapse_report(old_arch_groups, path="old")
                + _collapse_report(new_arch_groups, path="new")
            ),
            side_effect_cross_repeat_collapses=(
                _collapse_report(old_side_groups, path="old")
                + _collapse_report(new_side_groups, path="new")
            ),
        )
    finally:
        cleanup_worktree(repo_path, worktree_path)


def _warn_if_repeats_imbalanced(repeats: int) -> None:
    """Log when ``repeats`` cannot be evenly counterbalanced between paths.

    Postconditions: logs a warning naming the exact old-first/new-first split
        when ``repeats > 1`` and odd (the per-repeat alternation in
        :func:`compare_submission` unavoidably gives one path one extra
        "runs first" slot — see the repeats-imbalance review comment this was
        added in response to); no-op otherwise. Never raises.
    """
    if repeats > 1 and repeats % 2 == 1:
        old_first = repeats // 2 + 1
        new_first = repeats // 2
        logger.warning(
            "compare_submission: repeats=%s is odd; the old path runs first %s time(s) vs "
            "%s for the merged path (cannot be exactly balanced) -- pass an even --repeats "
            "for a fully counterbalanced real run.",
            repeats,
            old_first,
            new_first,
        )


@dataclass
class ComparisonReport:
    results: List[SubmissionComparisonResult]

    def to_dict(self) -> dict:
        total_lost = sum(
            len(r.architecture_diff.lost) + len(r.side_effect_diff.lost) for r in self.results
        )
        total_added = sum(
            len(r.architecture_diff.added) + len(r.side_effect_diff.added) for r in self.results
        )
        total_collapses = sum(
            len(r.architecture_cross_repeat_collapses) + len(r.side_effect_cross_repeat_collapses)
            for r in self.results
        )
        return {
            "summary": {
                "submissions_compared": len(self.results),
                "total_lost_findings": total_lost,
                "total_added_findings": total_added,
                "total_cross_repeat_collapses": total_collapses,
                "total_old_llm_calls": sum(r.old_llm_calls for r in self.results),
                "total_new_llm_calls": sum(r.new_llm_calls for r in self.results),
                "total_old_call_failures": sum(r.old_call_failures for r in self.results),
                "total_new_call_failures": sum(r.new_call_failures for r in self.results),
            },
            "submissions": [r.to_dict() for r in self.results],
        }


def run_comparison(
    llm_factory: Callable[[], object],
    repo_path: Path,
    worktree_root: Path,
    corpus: Sequence[SubmissionSpec] = CORPUS,
    repeats: int = 1,
) -> ComparisonReport:
    """Compare every corpus submission, one after another.

    Postconditions: returns a :class:`ComparisonReport` covering every entry
        in ``corpus``; one submission's ``SnapshotComparisonError`` propagates
        (a corpus/environment problem should stop the run, not silently
        shrink the comparison — the caller decides whether to narrow
        ``corpus`` and retry).
    """
    results = [
        compare_submission(spec, llm_factory, repo_path, worktree_root, repeats=repeats)
        for spec in corpus
    ]
    return ComparisonReport(results=results)


def _render_summary(report: ComparisonReport) -> str:
    """Human-readable one-screen summary for the CLI's stdout."""
    lines = ["Snapshot comparison summary", "=" * 28]
    for r in report.results:
        lines.append(
            f"- {r.label}: "
            f"architecture matched={len(r.architecture_diff.matched)} "
            f"lost={len(r.architecture_diff.lost)} added={len(r.architecture_diff.added)} | "
            f"side-effect matched={len(r.side_effect_diff.matched)} "
            f"lost={len(r.side_effect_diff.lost)} added={len(r.side_effect_diff.added)}"
        )
    summary = report.to_dict()["summary"]
    lines.append("")
    lines.append(
        f"Totals: {summary['submissions_compared']} submission(s), "
        f"{summary['total_lost_findings']} lost, {summary['total_added_findings']} added, "
        f"{summary['total_cross_repeat_collapses']} cross-repeat collapse(s) to audit, "
        f"{summary['total_old_llm_calls']} old-path calls vs "
        f"{summary['total_new_llm_calls']} new-path calls, "
        f"{summary['total_old_call_failures']} old-path / "
        f"{summary['total_new_call_failures']} new-path call failure(s) "
        "fail-safe-degraded to empty findings."
    )
    return "\n".join(lines)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare the pre-consolidation two-call architecture/side-effect "
            "findings against the merged one-call pass, over a corpus of real "
            "past submissions. See docs/snapshot-comparison-report.md."
        )
    )
    parser.add_argument(
        "--repo-root",
        required=True,
        help="Path to a git checkout of this repository that has the corpus commits.",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="Runs per path per submission (mitigates LLM sampling variance). Default 1.",
    )
    parser.add_argument(
        "--output",
        default="snapshot_comparison_report.json",
        help="Path to write the JSON report to.",
    )
    parser.add_argument(
        "--dummy",
        action="store_true",
        help=(
            "Smoke-test mode: use DummyLLMClient instead of a real provider. "
            "Proves the worktree checkout + both call paths + diffing plumbing "
            "work end-to-end. Produces NO meaningful regression signal — a "
            "scripted stub cannot distinguish one call from two."
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = _build_arg_parser().parse_args(argv)
    repo_path = Path(args.repo_root).resolve()
    worktree_root = repo_path.parent / f".{repo_path.name}.snapshot-comparison-worktrees"
    worktree_root.mkdir(parents=True, exist_ok=True)

    if args.dummy:
        from llm_service.clients.dummy import DummyLLMClient

        def llm_factory() -> object:
            return DummyLLMClient()
    else:
        from llm_service import get_client

        def llm_factory() -> object:
            return get_client("code_review")

    report = run_comparison(llm_factory, repo_path, worktree_root, repeats=args.repeats)
    Path(args.output).write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    print(_render_summary(report))
    print(f"\nFull report written to {args.output}")


if __name__ == "__main__":
    main()
