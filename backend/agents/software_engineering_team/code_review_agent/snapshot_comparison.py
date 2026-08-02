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

from shared.git.git_utils import add_worktree, remove_worktree
from software_engineering_team.shared.models import SystemArchitecture

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


def run_two_call(
    llm: object,
    input_data: CodeReviewInput,
    repo_reader: Optional[RepoReader],
    index: CodebaseIndex,
) -> Tuple[List[CodeReviewIssue], List[CodeReviewIssue]]:
    """Run the pre-consolidation path: two independent per-submission calls.

    Postconditions: returns ``(architecture_findings, side_effect_findings)``,
        identical in shape to :func:`run_merged_call`'s return, calling the
        standalone passes exactly as ``coordinator._run_tail_passes`` did
        before the merge (recoverable verbatim from commit ``358873b^``).
    """
    architecture = find_architecture_and_redundancy_issues(
        llm, input_data, repo_reader=repo_reader, index=index
    )
    side_effect = find_side_effect_impact_issues(
        llm, input_data, repo_reader=repo_reader, index=index
    )
    return architecture, side_effect


def run_merged_call(
    llm: object,
    input_data: CodeReviewInput,
    repo_reader: Optional[RepoReader],
    index: CodebaseIndex,
) -> Tuple[List[CodeReviewIssue], List[CodeReviewIssue]]:
    """Run the post-consolidation path: one merged per-submission call.

    Postconditions: returns ``(architecture_findings, side_effect_findings)``
        exactly as ``find_architecture_and_side_effect_issues`` does.
    """
    return find_architecture_and_side_effect_issues(
        llm, input_data, repo_reader=repo_reader, index=index
    )


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

    Postconditions: returns 0.0 when categories differ or both cite a
        non-blank, different ``file_path``; otherwise a
        ``difflib.SequenceMatcher`` ratio over ``description``, halved when
        both cite a ``line`` more than ``_MATCH_LINE_TOLERANCE`` apart. Pure;
        never raises.
    """
    if a.category != b.category:
        return 0.0
    if a.file_path and b.file_path and a.file_path != b.file_path:
        return 0.0
    score = difflib.SequenceMatcher(None, a.description.lower(), b.description.lower()).ratio()
    if a.line is not None and b.line is not None and abs(a.line - b.line) > _MATCH_LINE_TOLERANCE:
        score *= 0.5
    return score


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

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "old_llm_calls": self.old_llm_calls,
            "new_llm_calls": self.new_llm_calls,
            "architecture": self.architecture_diff.to_dict(),
            "side_effect": self.side_effect_diff.to_dict(),
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
          are computed over the POOLED findings across all ``repeats`` runs of
          each path (not diffed run-by-run), so a finding that appears in any
          repeat counts once its diff is inspected as "old found it" /
          "new found it" (repeats reduce false 'lost' calls from one
          unlucky sample, at the cost of also pooling any duplicate the model
          emits across runs — a human reviewing ``lost``/``added`` should
          expect some near-duplicates from this pooling, not treat every
          entry as a distinct finding).
        - Which path runs first alternates by repeat index (old-then-new on
          even repeats, new-then-old on odd repeats), so a rate-limited or
          time-degrading provider does not systematically disadvantage
          whichever path always ran second within a repeat — an ordering
          bias that repeating with a fixed order would amplify, not average
          out.
        - The worktree is always removed, even when a pass call raises.
    """
    assert repeats >= 1, "repeats must be >= 1"
    input_data, repo_reader, worktree_path = materialize_submission(spec, repo_path, worktree_root)
    try:
        index = CodebaseIndex.from_input(input_data, repo_reader=repo_reader)
        old_arch: List[CodeReviewIssue] = []
        old_side: List[CodeReviewIssue] = []
        new_arch: List[CodeReviewIssue] = []
        new_side: List[CodeReviewIssue] = []
        for i in range(repeats):
            steps = ["old", "new"] if i % 2 == 0 else ["new", "old"]
            for step in steps:
                if step == "old":
                    a, s = run_two_call(llm_factory(), input_data, repo_reader, index)
                    old_arch.extend(a)
                    old_side.extend(s)
                else:
                    a2, s2 = run_merged_call(llm_factory(), input_data, repo_reader, index)
                    new_arch.extend(a2)
                    new_side.extend(s2)
        return SubmissionComparisonResult(
            label=spec.label,
            architecture_diff=diff_findings(old_arch, new_arch),
            side_effect_diff=diff_findings(old_side, new_side),
            old_llm_calls=repeats * 2,
            new_llm_calls=repeats * 1,
        )
    finally:
        cleanup_worktree(repo_path, worktree_path)


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
        return {
            "summary": {
                "submissions_compared": len(self.results),
                "total_lost_findings": total_lost,
                "total_added_findings": total_added,
                "total_old_llm_calls": sum(r.old_llm_calls for r in self.results),
                "total_new_llm_calls": sum(r.new_llm_calls for r in self.results),
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
        f"{summary['total_old_llm_calls']} old-path calls vs "
        f"{summary['total_new_llm_calls']} new-path calls."
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
