"""Tests for the architecture/side-effect snapshot-comparison harness.

Fully offline: the corpus-sanity tests do plain structural checks (no network,
no LLM), and the wiring test builds a throwaway one-commit local git repo
(``git init``/``git commit``, no network) so :func:`compare_submission`'s real
``git worktree`` + both call-path + diffing plumbing is exercised end-to-end
against a scripted ``DummyLLMClient`` double -- mirroring
``test_merged_architecture_side_effect_pass.py``'s anchor-matching convention.
This does NOT exercise real LLM behavior (a scripted stub answers identically
regardless of call count) -- see ``docs/snapshot-comparison-report.md`` for
how to run a real comparison.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict

from code_review_agent.models import CodeReviewIssue
from code_review_agent.snapshot_comparison import (
    CORPUS,
    FindingDiff,
    SubmissionSpec,
    _dedupe_pooled,
    _finding_similarity,
    compare_submission,
    diff_findings,
)

from llm_service.clients.dummy import DummyLLMClient

# Same anchor convention as test_architecture_consistency_pass.py /
# test_side_effect_impact_pass.py / test_merged_architecture_side_effect_pass.py:
# branch on the user prompt only, never the system prompt.
_ARCH_PASS_ANCHOR = '"findings" array as instructed'
_SIDE_EFFECT_PASS_ANCHOR = '"side-effects"/"documentation" findings array'
_MERGED_PASS_ANCHOR = '"architecture_findings"/"side_effect_findings"'


def _issue(**overrides: object) -> CodeReviewIssue:
    defaults: Dict[str, object] = dict(
        severity="medium",
        category="architecture",
        file_path="app.py",
        line=10,
        description="finding description",
        suggestion="do something about it",
    )
    defaults.update(overrides)
    return CodeReviewIssue(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# diff_findings / _finding_similarity
# ---------------------------------------------------------------------------


def test_diff_findings_matches_similar_wording_same_file_and_category() -> None:
    old = [_issue(description="bypasses the repository layer entirely")]
    new = [_issue(description="bypasses the repository layer")]
    result = diff_findings(old, new)
    assert len(result.matched) == 1
    assert result.lost == []
    assert result.added == []


def test_diff_findings_reports_lost_when_old_has_no_counterpart() -> None:
    old = [_issue(description="stale docstring on foo()")]
    result = diff_findings(old, [])
    assert result.lost == old
    assert result.matched == []
    assert result.added == []


def test_diff_findings_reports_added_when_new_has_no_counterpart() -> None:
    new = [_issue(description="a finding never seen on the old path")]
    result = diff_findings([], new)
    assert result.added == new
    assert result.matched == []
    assert result.lost == []


def test_diff_findings_never_matches_across_categories() -> None:
    old = [_issue(category="architecture", description="identical wording")]
    new = [_issue(category="side-effects", description="identical wording")]
    result = diff_findings(old, new)
    assert result.matched == []
    assert result.lost == old
    assert result.added == new


def test_diff_findings_never_matches_different_nonblank_files() -> None:
    old = [_issue(file_path="a.py", description="identical wording")]
    new = [_issue(file_path="b.py", description="identical wording")]
    result = diff_findings(old, new)
    assert result.matched == []


def test_diff_findings_greedily_pairs_best_scoring_candidate() -> None:
    old = [_issue(description="caller assumes the old return type of parse()")]
    new = [
        _issue(description="totally unrelated finding about naming"),
        _issue(description="caller assumes the old return type of parse"),
    ]
    result = diff_findings(old, new)
    assert len(result.matched) == 1
    matched_new = result.matched[0][1]
    assert "return type of parse" in matched_new.description
    assert len(result.added) == 1


def test_dedupe_pooled_collapses_the_same_finding_repeated_across_repeats() -> None:
    repeat_0 = [_issue(description="bypasses the repository layer")]
    repeat_1 = [_issue(description="bypasses the repository layer entirely")]
    repeat_2 = [_issue(description="bypasses the repository layer here too")]
    deduped = _dedupe_pooled([repeat_0, repeat_1, repeat_2])
    assert deduped == [repeat_0[0]]


def test_dedupe_pooled_keeps_genuinely_different_findings_separate() -> None:
    repeat_0 = [
        _issue(description="bypasses the repository layer"),
        _issue(description="stale docstring drift on an unrelated function"),
    ]
    assert _dedupe_pooled([repeat_0]) == repeat_0


def test_dedupe_pooled_never_collapses_two_findings_from_the_same_repeat() -> None:
    """A single run can legitimately emit two distinct findings that happen to
    share wording (e.g. the same violation on two different code paths in one
    file) -- these must both survive, even though they score above the match
    threshold against each other. Only a finding from a LATER repeat may
    collapse against one already kept from an earlier repeat."""
    finding_a = _issue(description="bypasses the repository layer for reads")
    finding_b = _issue(description="bypasses the repository layer for writes")
    assert _finding_similarity(finding_a, finding_b) >= 0.45  # sanity: they WOULD collide

    same_repeat = [finding_a, finding_b]
    assert _dedupe_pooled([same_repeat]) == [finding_a, finding_b]

    # But a near-duplicate of finding_a from a later, separate repeat still collapses.
    later_repeat = [_issue(description="bypasses the repository layer for reads, too")]
    assert _dedupe_pooled([same_repeat, later_repeat]) == [finding_a, finding_b]


def test_finding_similarity_zero_for_different_category() -> None:
    a = _issue(category="architecture")
    b = _issue(category="side-effects")
    assert _finding_similarity(a, b) == 0.0


def test_finding_similarity_zero_for_different_nonblank_file_path() -> None:
    a = _issue(file_path="a.py")
    b = _issue(file_path="b.py")
    assert _finding_similarity(a, b) == 0.0


def test_finding_similarity_blank_file_path_never_blocks_a_match() -> None:
    a = _issue(file_path="", description="same wording here")
    b = _issue(file_path="app.py", description="same wording here")
    assert _finding_similarity(a, b) > 0.0


def test_finding_similarity_is_reduced_by_distant_line() -> None:
    text = "caller assumes old behavior at this exact call site"
    near = _finding_similarity(_issue(line=10, description=text), _issue(line=12, description=text))
    far = _finding_similarity(_issue(line=10, description=text), _issue(line=200, description=text))
    assert far < near


# ---------------------------------------------------------------------------
# CORPUS sanity (structural only -- no network, no git, no LLM)
# ---------------------------------------------------------------------------


def test_corpus_is_nonempty() -> None:
    assert len(CORPUS) > 0


def test_every_corpus_entry_has_changed_files() -> None:
    for spec in CORPUS:
        assert spec.changed_files, f"{spec.label!r} has no changed_files"


def test_corpus_labels_are_unique() -> None:
    labels = [spec.label for spec in CORPUS]
    assert len(labels) == len(set(labels)), "duplicate label in CORPUS"


def test_corpus_commit_shas_are_full_hex_shas() -> None:
    for spec in CORPUS:
        assert len(spec.commit_sha) == 40, f"{spec.label!r}: not a full SHA: {spec.commit_sha!r}"
        int(spec.commit_sha, 16)  # raises ValueError if not hex


def test_corpus_covers_with_and_without_architecture_doc() -> None:
    flags = {spec.with_architecture_doc for spec in CORPUS}
    assert flags == {True, False}, "corpus must cover both the with-doc and without-doc axis"


# ---------------------------------------------------------------------------
# compare_submission wiring (real local git worktree, scripted LLM double)
# ---------------------------------------------------------------------------


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _init_one_commit_repo(tmp_path: Path) -> "tuple[Path, str]":
    """Build a throwaway one-commit local git repo (no network) for the harness to check out."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("def bar():\n    return 1\n", encoding="utf-8")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-q", "-m", "init")
    sha = _run_git(repo, "rev-parse", "HEAD")
    return repo, sha


class _ScriptedComparisonClient(DummyLLMClient):
    """Old path: architecture + side-effect findings. Merged path: architecture only.

    Deliberately drops the side-effect finding on the merged path so the test
    proves the harness surfaces a dropped finding as ``lost``, not silently.
    """

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        if _MERGED_PASS_ANCHOR in prompt:
            return {
                "architecture_findings": [
                    {
                        "severity": "high",
                        "category": "architecture",
                        "file_path": "app.py",
                        "description": "bypasses the repository layer",
                        "suggestion": "use the repository",
                    }
                ],
                "side_effect_findings": [],
            }
        if _ARCH_PASS_ANCHOR in prompt:
            return {
                "findings": [
                    {
                        "severity": "high",
                        "category": "architecture",
                        "file_path": "app.py",
                        "description": "bypasses the repository layer entirely",
                        "suggestion": "use the repository",
                    }
                ]
            }
        if _SIDE_EFFECT_PASS_ANCHOR in prompt:
            return {
                "findings": [
                    {
                        "severity": "medium",
                        "category": "side-effects",
                        "file_path": "app.py",
                        "description": "caller at other.py assumes the old return value",
                        "suggestion": "update the caller",
                    }
                ]
            }
        return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}


def test_compare_submission_calls_two_paths_and_surfaces_a_dropped_finding(tmp_path: Path) -> None:
    repo, sha = _init_one_commit_repo(tmp_path)
    spec = SubmissionSpec(
        label="synthetic",
        commit_sha=sha,
        changed_files=("app.py",),
        task_description="test change",
    )

    result = compare_submission(
        spec,
        lambda: _ScriptedComparisonClient(),
        repo,
        tmp_path / "worktrees",
        repeats=1,
    )

    assert result.old_llm_calls == 2
    assert result.new_llm_calls == 1
    # Architecture: near-identical wording on both paths -> matched, nothing lost/added.
    assert isinstance(result.architecture_diff, FindingDiff)
    assert len(result.architecture_diff.matched) == 1
    assert result.architecture_diff.lost == []
    assert result.architecture_diff.added == []
    # Side-effect: only the old path found it -> the merge dropped it. The harness
    # must surface this as `lost`, which is exactly the signal a real snapshot
    # comparison run would use to flag a regression.
    assert len(result.side_effect_diff.lost) == 1
    assert result.side_effect_diff.matched == []

    # The worktree this call created must be cleaned up, not left behind.
    worktrees_dir = tmp_path / "worktrees"
    if worktrees_dir.exists():
        assert list(worktrees_dir.iterdir()) == []


def test_compare_submission_to_dict_is_json_serializable(tmp_path: Path) -> None:
    import json

    repo, sha = _init_one_commit_repo(tmp_path)
    spec = SubmissionSpec(
        label="synthetic",
        commit_sha=sha,
        changed_files=("app.py",),
        task_description="test change",
    )
    result = compare_submission(
        spec, lambda: _ScriptedComparisonClient(), repo, tmp_path / "worktrees", repeats=1
    )
    json.dumps(result.to_dict())  # must not raise


def test_compare_submission_alternates_which_path_runs_first_across_repeats(
    tmp_path: Path,
) -> None:
    """A rate-limited/degrading real provider must not systematically penalize
    whichever path always ran second within a repeat -- see the P1 review
    comment this test was added in response to."""
    repo, sha = _init_one_commit_repo(tmp_path)
    spec = SubmissionSpec(
        label="synthetic-order",
        commit_sha=sha,
        changed_files=("app.py",),
        task_description="test change",
    )
    call_order: list[str] = []

    class _OrderTrackingClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in prompt:
                call_order.append("merged")
                return {"architecture_findings": [], "side_effect_findings": []}
            if _ARCH_PASS_ANCHOR in prompt:
                call_order.append("arch")
                return {"findings": []}
            if _SIDE_EFFECT_PASS_ANCHOR in prompt:
                call_order.append("side")
                return {"findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    compare_submission(
        spec, lambda: _OrderTrackingClient(), repo, tmp_path / "worktrees", repeats=2
    )

    # Repeat 0 (even): old path (arch, side) then new path (merged).
    # Repeat 1 (odd): new path (merged) then old path (arch, side).
    assert call_order == ["arch", "side", "merged", "merged", "arch", "side"]


def test_compare_submission_dedupes_findings_pooled_unevenly_across_repeats(
    tmp_path: Path,
) -> None:
    """The old path finds the same real finding on every repeat; the merged path
    only finds it on one of three repeats. Without dedup, the 1:1 matcher would
    pair one old copy with the merged copy and report the other two old copies
    as spurious `lost` entries -- see the P1 review comment this test was added
    in response to."""
    repo, sha = _init_one_commit_repo(tmp_path)
    spec = SubmissionSpec(
        label="synthetic-uneven-repeats",
        commit_sha=sha,
        changed_files=("app.py",),
        task_description="test change",
    )
    merged_calls = {"n": 0}
    _FINDING = {
        "severity": "high",
        "category": "architecture",
        "file_path": "app.py",
        "description": "bypasses the repository layer",
        "suggestion": "use the repository",
    }

    class _UnevenClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in prompt:
                merged_calls["n"] += 1
                findings = [_FINDING] if merged_calls["n"] == 1 else []
                return {"architecture_findings": findings, "side_effect_findings": []}
            if _ARCH_PASS_ANCHOR in prompt:
                return {"findings": [_FINDING]}
            if _SIDE_EFFECT_PASS_ANCHOR in prompt:
                return {"findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    result = compare_submission(
        spec, lambda: _UnevenClient(), repo, tmp_path / "worktrees", repeats=3
    )

    assert len(result.architecture_diff.matched) == 1
    assert result.architecture_diff.lost == []
    assert result.architecture_diff.added == []
