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

import logging
import subprocess
from pathlib import Path
from typing import Any, Dict

import pytest
from code_review_agent.models import CodeReviewIssue
from code_review_agent.snapshot_comparison import (
    CORPUS,
    FindingDiff,
    SnapshotComparisonError,
    SubmissionSpec,
    _call_pass_detecting_failure,
    _collapse_report,
    _dedupe_pooled,
    _DedupeGroup,
    _finding_similarity,
    _require_passes_enabled,
    _warn_if_repeats_imbalanced,
    compare_submission,
    diff_findings,
)
from tests.submission_pass_two_call_client import (
    SubmissionPassTwoCallClient,
    wire_run_agent_via_reasoning_for_test_clients,
)

from llm_service.clients.dummy import DummyLLMClient


@pytest.fixture(autouse=True)
def _wire_submission_pass_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the submission-pass runner's ``run_agent_via_reasoning`` through the
    two-call test stub for every test in this module.

    File-scoped (a plain module-level fixture, not a ``pytest_plugins``
    registration): a fixture defined directly in a test module only applies to
    that module's own tests, so this cannot leak into sibling test files under
    pytest-xdist the way a ``pytest_plugins`` registration would (each xdist
    worker collects the whole test tree, so a session-wide plugin's autouse
    fixtures would otherwise apply to every test the worker runs).
    """
    import code_review_agent.submission_pass_runner as runner_mod

    wire_run_agent_via_reasoning_for_test_clients(monkeypatch, runner_mod)


# Same anchor convention as test_architecture_consistency_pass.py /
# test_side_effect_impact_pass.py / test_merged_architecture_side_effect_pass.py:
# branch on the reasoning-pass user prompt only, never the format wrap or
# the system prompt.
_ARCH_PASS_ANCHOR = "Summarize architecture-consistency findings in structured prose"
_SIDE_EFFECT_PASS_ANCHOR = "Summarize side-effect-impact findings in structured prose"
_MERGED_PASS_ANCHOR = "Merged submission pass:"


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


def test_diff_findings_never_matches_identical_text_at_distant_lines() -> None:
    old = [_issue(line=10, description="stale docstring drift")]
    new = [_issue(line=200, description="stale docstring drift")]
    result = diff_findings(old, new)
    assert result.matched == []
    assert result.lost == old
    assert result.added == new


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
    groups = _dedupe_pooled([repeat_0, repeat_1, repeat_2])
    assert len(groups) == 1
    assert groups[0].representative == repeat_0[0]
    # Every collapsed original is kept for audit, not silently discarded.
    assert groups[0].collapsed == [repeat_1[0], repeat_2[0]]


def test_dedupe_pooled_keeps_genuinely_different_findings_separate() -> None:
    repeat_0 = [
        _issue(description="bypasses the repository layer"),
        _issue(description="stale docstring drift on an unrelated function"),
    ]
    groups = _dedupe_pooled([repeat_0])
    assert [g.representative for g in groups] == repeat_0
    assert all(g.collapsed == [] for g in groups)


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
    groups = _dedupe_pooled([same_repeat])
    assert [g.representative for g in groups] == [finding_a, finding_b]
    assert all(g.collapsed == [] for g in groups)

    # A near-duplicate of finding_a from a later, separate repeat still
    # collapses -- but the original is recorded on the matching group for
    # audit, never silently dropped (this is exactly the case a pure
    # similarity heuristic cannot distinguish from a genuinely distinct
    # finding that happens to land in a different repeat).
    later_repeat = [_issue(description="bypasses the repository layer for reads, too")]
    groups2 = _dedupe_pooled([same_repeat, later_repeat])
    assert [g.representative for g in groups2] == [finding_a, finding_b]
    assert groups2[0].collapsed == later_repeat
    assert groups2[1].collapsed == []


def test_dedupe_pooled_consumes_at_most_one_finding_per_group_per_run() -> None:
    """A later repeat that emits both a real recurrence of an earlier finding
    AND an unrelated finding that also happens to resemble that same earlier
    representative must not collapse both into it -- only the best match
    consumes the group; the other becomes its own new representative rather
    than being silently discarded."""
    read_violation = _issue(description="bypasses the repository layer for reads")
    repeat_0 = [read_violation]

    recurrence = _issue(description="bypasses the repository layer for reads again")
    write_violation = _issue(description="bypasses the repository layer for writes")
    # Sanity: write_violation WOULD also match read_violation's group if
    # matching weren't one-to-one within the run.
    assert _finding_similarity(write_violation, read_violation) >= 0.45
    repeat_1 = [recurrence, write_violation]

    groups = _dedupe_pooled([repeat_0, repeat_1])

    assert len(groups) == 2
    assert groups[0].representative == read_violation
    assert groups[0].collapsed == [recurrence]
    assert groups[1].representative == write_violation
    assert groups[1].collapsed == []


def test_collapse_report_labels_each_group_with_its_source_path() -> None:
    finding = _issue(description="bypasses the repository layer")
    dup = _issue(description="bypasses the repository layer entirely")
    groups = [_DedupeGroup(representative=finding, collapsed=[dup])]

    report = _collapse_report(groups, path="old")

    assert len(report) == 1
    assert report[0]["path"] == "old"
    assert report[0]["representative"] == finding.model_dump()
    assert report[0]["collapsed"] == [dup.model_dump()]


def test_collapse_report_omits_groups_with_nothing_collapsed() -> None:
    groups = [_DedupeGroup(representative=_issue())]
    assert _collapse_report(groups, path="new") == []


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


def test_finding_similarity_rejects_distant_lines_even_with_identical_text() -> None:
    """A score reduction alone is not enough to keep two findings with
    identical text at genuinely different locations from clearing
    _MATCH_TEXT_THRESHOLD -- this must be a hard 0.0, not a penalty."""
    text = "caller assumes old behavior at this exact call site"
    near = _finding_similarity(_issue(line=10, description=text), _issue(line=12, description=text))
    far = _finding_similarity(_issue(line=10, description=text), _issue(line=200, description=text))
    assert near > 0.0
    assert far == 0.0


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


class _ScriptedComparisonClient(SubmissionPassTwoCallClient):
    """Old path: architecture + side-effect findings. Merged path: architecture only.

    Deliberately drops the side-effect finding on the merged path so the test
    proves the harness surfaces a dropped finding as ``lost``, not silently.
    """

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        reasoning = self.latest_reasoning_prompt()
        if _MERGED_PASS_ANCHOR in reasoning:
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
        if _ARCH_PASS_ANCHOR in reasoning:
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
        if _SIDE_EFFECT_PASS_ANCHOR in reasoning:
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

    class _OrderTrackingClient(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            reasoning = self.latest_reasoning_prompt()
            if _MERGED_PASS_ANCHOR in reasoning:
                call_order.append("merged")
                return {"architecture_findings": [], "side_effect_findings": []}
            if _ARCH_PASS_ANCHOR in reasoning:
                call_order.append("arch")
                return {"findings": []}
            if _SIDE_EFFECT_PASS_ANCHOR in reasoning:
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

    class _UnevenClient(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            reasoning = self.latest_reasoning_prompt()
            if _MERGED_PASS_ANCHOR in reasoning:
                merged_calls["n"] += 1
                findings = [_FINDING] if merged_calls["n"] == 1 else []
                return {"architecture_findings": findings, "side_effect_findings": []}
            if _ARCH_PASS_ANCHOR in reasoning:
                return {"findings": [_FINDING]}
            if _SIDE_EFFECT_PASS_ANCHOR in reasoning:
                return {"findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    result = compare_submission(
        spec, lambda: _UnevenClient(), repo, tmp_path / "worktrees", repeats=3
    )

    assert len(result.architecture_diff.matched) == 1
    assert result.architecture_diff.lost == []
    assert result.architecture_diff.added == []

    # The old path's 2 collapsed cross-repeat copies are recorded for audit,
    # not silently discarded (the new path found it consistently in only 1
    # repeat, so it has nothing to collapse).
    assert len(result.architecture_cross_repeat_collapses) == 1
    assert len(result.architecture_cross_repeat_collapses[0]["collapsed"]) == 2
    import json

    json.dumps(result.to_dict())  # must stay JSON-serializable


# ---------------------------------------------------------------------------
# _warn_if_repeats_imbalanced
# ---------------------------------------------------------------------------


def test_warn_if_repeats_imbalanced_logs_for_odd_repeats_greater_than_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="code_review_agent.snapshot_comparison"):
        _warn_if_repeats_imbalanced(3)
    assert any("repeats=3 is odd" in r.message for r in caplog.records)


def test_warn_if_repeats_imbalanced_silent_for_single_or_even_repeats(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="code_review_agent.snapshot_comparison"):
        _warn_if_repeats_imbalanced(1)
        _warn_if_repeats_imbalanced(4)
    assert caplog.records == []


# ---------------------------------------------------------------------------
# _call_pass_detecting_failure / call-failure tracking
# ---------------------------------------------------------------------------

_ARCH_PASS_LOGGER = "code_review_agent.architecture_consistency_pass"


def test_call_pass_detecting_failure_true_when_target_logger_warns() -> None:
    def _logs_a_warning() -> list:
        logging.getLogger(_ARCH_PASS_LOGGER).warning("ArchitectureConsistencyPass: failed (boom)")
        return []

    result, failed = _call_pass_detecting_failure(_logs_a_warning, _ARCH_PASS_LOGGER)
    assert result == []
    assert failed is True


def test_call_pass_detecting_failure_false_when_silent() -> None:
    def _clean() -> list:
        return ["finding"]

    result, failed = _call_pass_detecting_failure(_clean, _ARCH_PASS_LOGGER)
    assert result == ["finding"]
    assert failed is False


def test_call_pass_detecting_failure_ignores_unrelated_loggers() -> None:
    def _warns_elsewhere() -> list:
        logging.getLogger("code_review_agent.side_effect_impact_pass").warning("unrelated failure")
        return []

    _, failed = _call_pass_detecting_failure(_warns_elsewhere, _ARCH_PASS_LOGGER)
    assert failed is False


def test_compare_submission_detects_and_counts_call_failures(tmp_path: Path) -> None:
    """The old path's architecture call fails on every repeat (simulating a
    real provider error) while the merged path never fails; the harness must
    surface this rather than silently pooling the fail-safe empty result as
    a genuine 'found nothing'."""
    repo, sha = _init_one_commit_repo(tmp_path)
    spec = SubmissionSpec(
        label="synthetic-failure",
        commit_sha=sha,
        changed_files=("app.py",),
        task_description="test change",
    )

    class _FlakyClient(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            reasoning = self.latest_reasoning_prompt()
            if _ARCH_PASS_ANCHOR in reasoning:
                raise RuntimeError("simulated transient failure")
            if _SIDE_EFFECT_PASS_ANCHOR in reasoning:
                return {"findings": []}
            if _MERGED_PASS_ANCHOR in reasoning:
                return {"architecture_findings": [], "side_effect_findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    result = compare_submission(
        spec, lambda: _FlakyClient(), repo, tmp_path / "worktrees", repeats=1
    )

    assert result.old_call_failures == 1  # the architecture half only; side-effect succeeded
    assert result.new_call_failures == 0
    import json

    json.dumps(result.to_dict())  # must stay JSON-serializable


def test_run_two_call_derives_logger_name_from_the_actual_pass_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: failure detection must key off the pass function's own
    ``__module__``, not a hard-coded guess at its import path.

    A hard-coded ``"code_review_agent.<pass>"`` string only matches when this
    package happens to be imported bare (e.g. under pytest's path
    insertion); imported through the fully-qualified
    ``software_engineering_team.code_review_agent`` package, the pass's real
    logger name differs and a hard-coded guess would silently never observe
    its failure warning. Simulate that mismatch by swapping in a fake pass
    function under an unrelated module name and confirming the failure is
    still detected.
    """
    from code_review_agent import snapshot_comparison as sc

    fake_module_name = "totally.unrelated.qualified.path.architecture_consistency_pass"

    def _fake_pass(llm: object, input_data: object, *, repo_reader: object, index: object) -> list:
        logging.getLogger(fake_module_name).warning("simulated fail-safe warning")
        return []

    _fake_pass.__module__ = fake_module_name
    monkeypatch.setattr(sc, "find_architecture_and_redundancy_issues", _fake_pass)
    # Keep the side-effect half a clean no-op so only the architecture fake's
    # forced failure is under test.
    monkeypatch.setattr(
        sc, "find_side_effect_impact_issues", lambda llm, input_data, *, repo_reader, index: []
    )

    _, _, architecture_failed, side_effect_failed = sc.run_two_call(
        llm=object(), input_data=object(), repo_reader=None, index=object()
    )

    assert architecture_failed is True
    assert side_effect_failed is False


# ---------------------------------------------------------------------------
# _require_passes_enabled
# ---------------------------------------------------------------------------


def test_require_passes_enabled_silent_by_default() -> None:
    _require_passes_enabled()  # must not raise


def test_require_passes_enabled_raises_when_architecture_pass_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS", "false")
    with pytest.raises(SnapshotComparisonError, match="CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS"):
        _require_passes_enabled()


def test_require_passes_enabled_raises_when_side_effect_pass_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS", "false")
    with pytest.raises(SnapshotComparisonError, match="CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS"):
        _require_passes_enabled()


def test_require_passes_enabled_names_both_when_both_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS", "false")
    monkeypatch.setenv("CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS", "false")
    with pytest.raises(SnapshotComparisonError) as exc_info:
        _require_passes_enabled()
    assert "CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS" in str(exc_info.value)
    assert "CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS" in str(exc_info.value)


def test_compare_submission_raises_when_a_pass_is_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A disabled pass would otherwise return [] on both call paths for every
    submission -- a silently 'clean' comparison for a category that was never
    actually exercised. compare_submission must refuse to run rather than
    produce that misleading report."""
    monkeypatch.setenv("CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS", "false")
    repo, sha = _init_one_commit_repo(tmp_path)
    spec = SubmissionSpec(
        label="synthetic-disabled-pass",
        commit_sha=sha,
        changed_files=("app.py",),
        task_description="test change",
    )
    with pytest.raises(SnapshotComparisonError):
        compare_submission(spec, lambda: DummyLLMClient(), repo, tmp_path / "worktrees", repeats=1)
