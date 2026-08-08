"""Direct unit tests for the small, isolated helpers in ``api/pr_review.py``.

Covers heartbeat liveness, duplicate-review and sibling-checkout detection,
language inference, the whole-file/hunk focus notes, author resolution, the
reviewer-dispatch/merge logic in ``_run_reviewer``, change-surface assembly,
and the per-file surface/whole-file/hunk partitioning in
``_decide_review_mode``. Self-contained: no imports from other test modules.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

import software_engineering_team.api.coding_team_main as main
from software_engineering_team.api import pr_review
from software_engineering_team.api.coding_team_state import _HEARTBEAT_CLOCK_SKEW_TOLERANCE_S
from software_engineering_team.code_review_agent.change_surface import ChangeSurface
from software_engineering_team.github_source import PullRequestFile
from software_engineering_team.models import JobStatus

_STALE_S = pr_review._REVIEW_GUARD_HEARTBEAT_STALE_S


def _iso(delta_seconds: float) -> str:
    """ISO timestamp ``delta_seconds`` from now (negative = future, positive = past)."""
    return (datetime.now(timezone.utc) - timedelta(seconds=delta_seconds)).isoformat()


# ---------------------------------------------------------------------------
# _review_job_heartbeat_live
# ---------------------------------------------------------------------------


class TestReviewJobHeartbeatLiveUnit:
    def test_none_job_is_live(self) -> None:
        assert pr_review._review_job_heartbeat_live(None) is True

    def test_missing_key_is_live(self) -> None:
        assert pr_review._review_job_heartbeat_live({}) is True

    def test_blank_timestamp_is_live(self) -> None:
        assert pr_review._review_job_heartbeat_live({"last_heartbeat_at": ""}) is True

    def test_unparseable_timestamp_is_live(self) -> None:
        assert pr_review._review_job_heartbeat_live({"last_heartbeat_at": "not-a-date"}) is True

    def test_naive_timestamp_within_window_is_live(self) -> None:
        naive = (datetime.now(timezone.utc) - timedelta(seconds=5)).replace(tzinfo=None)
        assert (
            pr_review._review_job_heartbeat_live({"last_heartbeat_at": naive.isoformat()}) is True
        )

    def test_fresh_aware_timestamp_is_live(self) -> None:
        job = {"last_heartbeat_at": _iso(0)}
        assert pr_review._review_job_heartbeat_live(job) is True

    def test_stale_timestamp_is_not_live(self) -> None:
        job = {"last_heartbeat_at": _iso(_STALE_S + 30)}
        assert pr_review._review_job_heartbeat_live(job) is False

    def test_timestamp_within_future_skew_tolerance_is_live(self) -> None:
        # A stamp comfortably inside the skew-tolerance margin still counts as live.
        job = {"last_heartbeat_at": _iso(-(_HEARTBEAT_CLOCK_SKEW_TOLERANCE_S - 2))}
        assert pr_review._review_job_heartbeat_live(job) is True

    def test_timestamp_past_future_skew_tolerance_is_not_live(self) -> None:
        job = {"last_heartbeat_at": _iso(-(_HEARTBEAT_CLOCK_SKEW_TOLERANCE_S + 10))}
        assert pr_review._review_job_heartbeat_live(job) is False


# ---------------------------------------------------------------------------
# _running_review_for_pr
# ---------------------------------------------------------------------------


def _job(owner="acme", repo="widgets", pr_number=7, job_id="job-1", heartbeat_delta=0.0):
    ctx: Dict[str, Any] = {"owner": owner, "repo": repo, "pr_number": pr_number}
    return {"job_id": job_id, "github_context": ctx, "last_heartbeat_at": _iso(heartbeat_delta)}


class TestRunningReviewForPrUnit:
    def test_no_jobs_returns_none(self, monkeypatch) -> None:
        monkeypatch.setattr(main, "list_jobs", lambda active_only=True: [])
        assert pr_review._running_review_for_pr("acme", "widgets", 7) is None

    def test_matching_live_job_returns_job_id(self, monkeypatch) -> None:
        monkeypatch.setattr(main, "list_jobs", lambda active_only=True: [_job()])
        assert pr_review._running_review_for_pr("acme", "widgets", 7) == "job-1"

    def test_string_pr_number_in_store_still_matches(self, monkeypatch) -> None:
        """Store round-trips may leave ``pr_number`` as a string; coerce before ==."""
        monkeypatch.setattr(main, "list_jobs", lambda active_only=True: [_job(pr_number="7")])
        assert pr_review._running_review_for_pr("acme", "widgets", 7) == "job-1"

    def test_non_numeric_pr_number_in_store_is_skipped(self, monkeypatch) -> None:
        monkeypatch.setattr(
            main, "list_jobs", lambda active_only=True: [_job(pr_number="not-a-number")]
        )
        assert pr_review._running_review_for_pr("acme", "widgets", 7) is None

    def test_overflow_pr_number_in_store_is_skipped(self, monkeypatch) -> None:
        """JSON-decoded ``1e309`` becomes ``float('inf')``; ``int()`` raises
        ``OverflowError``, which must skip the row rather than abort admission."""
        monkeypatch.setattr(
            main, "list_jobs", lambda active_only=True: [_job(pr_number=float("inf"))]
        )
        assert pr_review._running_review_for_pr("acme", "widgets", 7) is None

    def test_overflow_pr_number_does_not_abort_scan(self, monkeypatch) -> None:
        monkeypatch.setattr(
            main,
            "list_jobs",
            lambda active_only=True: [
                _job(pr_number=float("inf"), job_id="bad"),
                _job(),
            ],
        )
        assert pr_review._running_review_for_pr("acme", "widgets", 7) == "job-1"

    def test_owner_repo_match_is_case_insensitive(self, monkeypatch) -> None:
        monkeypatch.setattr(
            main, "list_jobs", lambda active_only=True: [_job(owner="Acme", repo="Widgets")]
        )
        assert pr_review._running_review_for_pr("acme", "widgets", 7) == "job-1"

    def test_non_matching_pr_number_returns_none(self, monkeypatch) -> None:
        monkeypatch.setattr(main, "list_jobs", lambda active_only=True: [_job(pr_number=99)])
        assert pr_review._running_review_for_pr("acme", "widgets", 7) is None

    def test_issue_run_context_not_matched(self, monkeypatch) -> None:
        issue_job = {
            "job_id": "job-2",
            "github_context": {"owner": "acme", "repo": "widgets", "issue_number": 7},
            "last_heartbeat_at": _iso(0),
        }
        monkeypatch.setattr(main, "list_jobs", lambda active_only=True: [issue_job])
        assert pr_review._running_review_for_pr("acme", "widgets", 7) is None

    def test_stale_match_marks_failed_and_returns_none(self, monkeypatch) -> None:
        stale = _job(heartbeat_delta=_STALE_S + 60)
        monkeypatch.setattr(main, "list_jobs", lambda active_only=True: [stale])
        job_updates: List[Dict[str, Any]] = []
        review_updates: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            main, "update_job", lambda job_id, **kw: job_updates.append({"job_id": job_id, **kw})
        )
        monkeypatch.setattr(
            main,
            "update_review",
            lambda job_id, **kw: review_updates.append({"job_id": job_id, **kw}),
        )

        assert pr_review._running_review_for_pr("acme", "widgets", 7) is None

        assert len(job_updates) == 1
        assert job_updates[0]["job_id"] == "job-1"
        assert job_updates[0]["status"] == "failed"
        assert "error" in job_updates[0]
        assert len(review_updates) == 1
        assert review_updates[0]["status"] == "failed"
        assert review_updates[0]["completed"] is True

    def test_stale_match_swallows_update_job_error(self, monkeypatch) -> None:
        stale = _job(heartbeat_delta=_STALE_S + 60)
        monkeypatch.setattr(main, "list_jobs", lambda active_only=True: [stale])

        def _boom(job_id, **kw):
            raise RuntimeError("job service unreachable")

        monkeypatch.setattr(main, "update_job", _boom)
        monkeypatch.setattr(main, "update_review", lambda *a, **kw: None)

        # Must not raise despite the job-service failure.
        assert pr_review._running_review_for_pr("acme", "widgets", 7) is None


# ---------------------------------------------------------------------------
# _running_sibling_on_checkout
# ---------------------------------------------------------------------------


class TestRunningSiblingOnCheckoutUnit:
    def test_no_jobs_returns_none(self, monkeypatch) -> None:
        monkeypatch.setattr(main, "list_jobs", lambda active_only=True: [])
        assert pr_review._running_sibling_on_checkout("/tmp/repo", "own-job") is None

    def test_sibling_with_same_path_is_returned(self, monkeypatch, tmp_path) -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        sibling = {"job_id": "sibling-1", "repo_path": str(repo_dir)}
        monkeypatch.setattr(main, "list_jobs", lambda active_only=True: [sibling])
        assert pr_review._running_sibling_on_checkout(str(repo_dir), "own-job") == sibling

    def test_own_job_is_excluded(self, monkeypatch, tmp_path) -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        own = {"job_id": "own-job", "repo_path": str(repo_dir)}
        monkeypatch.setattr(main, "list_jobs", lambda active_only=True: [own])
        assert pr_review._running_sibling_on_checkout(str(repo_dir), "own-job") is None

    def test_different_path_returns_none(self, monkeypatch, tmp_path) -> None:
        repo_dir = tmp_path / "repo"
        other_dir = tmp_path / "other"
        repo_dir.mkdir()
        other_dir.mkdir()
        sibling = {"job_id": "sibling-1", "repo_path": str(other_dir)}
        monkeypatch.setattr(main, "list_jobs", lambda active_only=True: [sibling])
        assert pr_review._running_sibling_on_checkout(str(repo_dir), "own-job") is None

    @pytest.mark.skipif(
        sys.platform == "win32", reason="Directory symlinks require privileges on Windows"
    )
    def test_symlinked_path_matches_canonically(self, monkeypatch, tmp_path) -> None:
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link = tmp_path / "link"
        os.symlink(real_dir, link, target_is_directory=(sys.platform == "win32"))
        sibling = {"job_id": "sibling-1", "repo_path": str(link)}
        monkeypatch.setattr(main, "list_jobs", lambda active_only=True: [sibling])
        assert pr_review._running_sibling_on_checkout(str(real_dir), "own-job") == sibling

    def test_falsy_repo_path_is_skipped(self, monkeypatch) -> None:
        job = {"job_id": "sibling-1", "repo_path": ""}
        monkeypatch.setattr(main, "list_jobs", lambda active_only=True: [job])
        assert pr_review._running_sibling_on_checkout("/tmp/repo", "own-job") is None

    def test_none_entry_is_skipped(self, monkeypatch) -> None:
        monkeypatch.setattr(main, "list_jobs", lambda active_only=True: [None])
        assert pr_review._running_sibling_on_checkout("/tmp/repo", "own-job") is None

    def test_list_jobs_failure_fail_closes_with_synthetic_sibling(self, monkeypatch) -> None:
        """A job-service scan failure must not raise — return a synthetic sibling
        so callers fail-close instead of mutating an unverified checkout."""
        secret_url = "https://x:ghp_LEAKEDTOKEN@github.com/o/r.git"

        def _boom(*, active_only: bool = True):
            raise RuntimeError(f"job service unreachable: {secret_url}")

        monkeypatch.setattr(main, "list_jobs", _boom)
        sibling = pr_review._running_sibling_on_checkout("/tmp/repo", "own-job")
        assert sibling is not None
        assert sibling["job_id"] == "<job-scan-unavailable>"
        assert sibling["repo_path"] == "/tmp/repo"


# ---------------------------------------------------------------------------
# _infer_review_language
# ---------------------------------------------------------------------------


class _FakeFile:
    def __init__(self, filename: str) -> None:
        self.filename = filename


class TestInferReviewLanguageUnit:
    def test_empty_defaults_to_python(self) -> None:
        assert pr_review._infer_review_language([]) == "python"

    def test_all_python_files(self) -> None:
        files = [_FakeFile("a.py"), _FakeFile("b.py")]
        assert pr_review._infer_review_language(files) == "python"

    def test_ts_family_files(self) -> None:
        files = [_FakeFile("a.ts"), _FakeFile("b.tsx"), _FakeFile("c.js"), _FakeFile("d.jsx")]
        assert pr_review._infer_review_language(files) == "typescript"

    def test_tie_defaults_to_python(self) -> None:
        files = [_FakeFile("a.py"), _FakeFile("b.ts")]
        assert pr_review._infer_review_language(files) == "python"

    def test_ts_outnumbering_py_wins(self) -> None:
        files = [_FakeFile("a.ts"), _FakeFile("b.ts"), _FakeFile("c.py")]
        assert pr_review._infer_review_language(files) == "typescript"

    def test_irrelevant_extensions_do_not_count(self) -> None:
        files = [_FakeFile("README.md"), _FakeFile("data.json")]
        assert pr_review._infer_review_language(files) == "python"


# ---------------------------------------------------------------------------
# _whole_file_focus
# ---------------------------------------------------------------------------


class TestWholeFileFocusUnit:
    def test_blank_body_returns_note_alone(self) -> None:
        result = pr_review._whole_file_focus("")
        assert result.startswith(pr_review.REVIEW_FOCUS_NOTE_PREFIX)
        assert "\n\n" not in result[: len(pr_review.REVIEW_FOCUS_NOTE_PREFIX) + 1]

    def test_whitespace_only_body_returns_note_alone(self) -> None:
        result = pr_review._whole_file_focus("   \n")
        assert result.startswith(pr_review.REVIEW_FOCUS_NOTE_PREFIX)
        assert "   " not in result

    def test_non_blank_body_is_prefixed_to_note(self) -> None:
        result = pr_review._whole_file_focus("Fixes the flaky retry loop.")
        assert result.startswith("Fixes the flaky retry loop.\n\n")
        assert pr_review.REVIEW_FOCUS_NOTE_PREFIX in result

    def test_note_instructs_pre_existing_field(self) -> None:
        result = pr_review._whole_file_focus("body")
        # Both directions of the tag, not just the bare field name -- a note
        # that only ever said "do not set pre_existing" would still contain
        # the word "pre_existing" but would fail this.
        assert "pre_existing: false" in result
        assert "pre_existing: true" in result


# ---------------------------------------------------------------------------
# _hunk_review_focus
# ---------------------------------------------------------------------------


class TestHunkReviewFocusUnit:
    def test_blank_body_returns_note_alone(self) -> None:
        result = pr_review._hunk_review_focus("")
        assert result.startswith(pr_review.REVIEW_FOCUS_NOTE_PREFIX)
        assert "\n\n" not in result[: len(pr_review.REVIEW_FOCUS_NOTE_PREFIX) + 1]

    def test_whitespace_only_body_returns_note_alone(self) -> None:
        result = pr_review._hunk_review_focus("   \n")
        assert result.startswith(pr_review.REVIEW_FOCUS_NOTE_PREFIX)
        assert "   " not in result

    def test_non_blank_body_is_prefixed_to_note(self) -> None:
        result = pr_review._hunk_review_focus("Fixes the flaky retry loop.")
        assert result.startswith("Fixes the flaky retry loop.\n\n")
        assert pr_review.REVIEW_FOCUS_NOTE_PREFIX in result

    def test_note_instructs_pre_existing_field(self) -> None:
        result = pr_review._hunk_review_focus("body")
        # Both directions of the tag, not just the bare field name -- a note
        # that only ever said "do not set pre_existing" would still contain
        # the word "pre_existing" but would fail this.
        assert "pre_existing: false" in result
        assert "pre_existing: true" in result

    def test_differs_from_whole_file_focus_with_hunk_specific_wording(self) -> None:
        # Guards against _hunk_review_focus regressing into a plain alias of
        # _whole_file_focus: the two must produce different text, and the hunk
        # note must mention hunks specifically (not just the shared
        # "pre_existing" tagging instructions the two modes share).
        hunk_result = pr_review._hunk_review_focus("body")
        whole_result = pr_review._whole_file_focus("body")
        assert hunk_result != whole_result
        assert "pre_existing" in hunk_result
        assert "hunk" in hunk_result.lower()
        assert "hunk" not in whole_result.lower()


# ---------------------------------------------------------------------------
# _review_author
# ---------------------------------------------------------------------------


class TestReviewAuthorUnit:
    def test_no_resolver_falls_back_to_anonymous(self, monkeypatch) -> None:
        monkeypatch.setattr(pr_review, "_resolve_author", None)
        assert pr_review._review_author() == "anonymous"

    def test_working_resolver_returns_its_value(self, monkeypatch) -> None:
        monkeypatch.setattr(pr_review, "_resolve_author", lambda: "octobot")
        assert pr_review._review_author() == "octobot"

    def test_raising_resolver_falls_back_to_anonymous(self, monkeypatch) -> None:
        def _boom():
            raise RuntimeError("no session")

        monkeypatch.setattr(pr_review, "_resolve_author", _boom)
        assert pr_review._review_author() == "anonymous"


# ---------------------------------------------------------------------------
# _join_nonblank / _MergedReviewerOutput
# ---------------------------------------------------------------------------


class TestJoinNonblankUnit:
    def test_empty_list_returns_empty_string(self) -> None:
        assert pr_review._join_nonblank([]) == ""

    def test_all_blank_entries_return_empty_string(self) -> None:
        assert pr_review._join_nonblank(["", "   ", "\n"]) == ""

    def test_single_nonblank_is_stripped(self) -> None:
        assert pr_review._join_nonblank(["  hello  "]) == "hello"

    def test_mixed_blank_and_nonblank_drops_blanks(self) -> None:
        assert pr_review._join_nonblank(["first", "", "second", "   "]) == "first\n\nsecond"

    def test_order_is_preserved(self) -> None:
        assert pr_review._join_nonblank(["a", "b", "c"]) == "a\n\nb\n\nc"


class _FakeOutput:
    """Minimal stand-in for a reviewer output object: `issues` is whatever list
    the test wants merged/returned verbatim (not necessarily real issue objects),
    `summary`/`spec_compliance_notes` are the free-text fields _MergedReviewerOutput
    joins across attempts."""

    def __init__(self, issues, summary, spec_compliance_notes) -> None:
        self.issues = issues
        self.summary = summary
        self.spec_compliance_notes = spec_compliance_notes


class TestMergedReviewerOutputUnit:
    def test_issues_are_concatenated_in_order(self) -> None:
        first = _FakeOutput(["issue-1"], "summary one", "")
        second = _FakeOutput(["issue-2", "issue-3"], "", "")
        merged = pr_review._MergedReviewerOutput([first, second])
        assert merged.issues == ["issue-1", "issue-2", "issue-3"]

    def test_summary_and_notes_drop_blank_entries(self) -> None:
        first = _FakeOutput([], "", "notes from whole-file pass")
        second = _FakeOutput([], "summary from hunk pass", "")
        merged = pr_review._MergedReviewerOutput([first, second])
        assert merged.summary == "summary from hunk pass"
        assert merged.spec_compliance_notes == "notes from whole-file pass"


# ---------------------------------------------------------------------------
# _run_reviewer
# ---------------------------------------------------------------------------


class _FakePR:
    def __init__(self, title: str = "Add feature", body: Optional[str] = "PR body") -> None:
        self.title = title
        self.body = body


class _RecordingProvider:
    """Queue-driven fake for ``provider.run_pr_code_review`` — records every call's kwargs."""

    def __init__(
        self, outputs: Optional[List[Any]] = None, error: Optional[Exception] = None
    ) -> None:
        self._outputs = list(outputs) if outputs is not None else []
        self._error = error
        self.calls: List[Dict[str, Any]] = []

    def run_pr_code_review(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._outputs.pop(0)


def _run_reviewer_kwargs(**overrides: Any) -> Dict[str, Any]:
    """Base kwargs for calling `pr_review._run_reviewer`, with sensible defaults
    for every parameter; pass any of them as a keyword to override."""
    base = dict(
        client=object(),
        owner="acme",
        repo="widgets",
        pr_number=7,
        job_id="job-1",
        pr=_FakePR(),
        files=[_FakeFile("a.py")],
        code="",
        head_files=None,
        repo_reader=None,
        change_surface=None,
    )
    base.update(overrides)
    return base


class TestRunReviewerUnit:
    def _patch_collaborators(self, monkeypatch) -> List[Dict[str, Any]]:
        outages: List[Dict[str, Any]] = []
        monkeypatch.setattr(main, "update_job", lambda job_id, **kw: None)
        monkeypatch.setattr(
            main,
            "_record_review_outage",
            lambda client, owner, repo, pr_number, job_id, message: outages.append(
                {
                    "owner": owner,
                    "repo": repo,
                    "pr_number": pr_number,
                    "job_id": job_id,
                    "message": message,
                }
            ),
        )
        return outages

    def test_only_head_files_runs_one_whole_file_call(self, monkeypatch) -> None:
        self._patch_collaborators(monkeypatch)
        output = _FakeOutput(["issue"], "summary", "notes")
        provider = _RecordingProvider([output])
        kwargs = _run_reviewer_kwargs(head_files={"a.py": "content"}, code="")

        result = pr_review._run_reviewer(provider, **kwargs)

        assert result is output
        assert len(provider.calls) == 1
        call = provider.calls[0]
        assert call["pre_numbered"] is False
        assert call["files"] == {"a.py": "content"}
        assert call["task_requirements"] == pr_review._whole_file_focus("PR body")
        # Content assertions so a simultaneous regression in _run_reviewer and
        # _whole_file_focus cannot hide behind the equality check above.
        assert "PR body" in call["task_requirements"]
        assert "pre_existing" in call["task_requirements"]

    def test_only_code_runs_one_hunk_call(self, monkeypatch) -> None:
        self._patch_collaborators(monkeypatch)
        output = _FakeOutput(["issue"], "summary", "notes")
        provider = _RecordingProvider([output])
        kwargs = _run_reviewer_kwargs(head_files=None, code="### a.py ###\n1: x = 1")

        result = pr_review._run_reviewer(provider, **kwargs)

        assert result is output
        assert len(provider.calls) == 1
        call = provider.calls[0]
        assert call["pre_numbered"] is True
        assert call["code"] == "### a.py ###\n1: x = 1"
        # Hunk mode now appends the same "tag pre-existing findings" focus note as
        # whole-file mode (previously it passed the PR body verbatim with no
        # tagging instruction) -- see _hunk_review_focus.
        assert call["task_requirements"] == pr_review._hunk_review_focus("PR body")
        # Content assertions (not just equality with _hunk_review_focus's own
        # output): guard against _run_reviewer regressing to pass the body
        # verbatim, which would make the equality check above vacuously true
        # only if _hunk_review_focus also regressed in lockstep.
        assert "PR body" in call["task_requirements"]
        assert "pre_existing" in call["task_requirements"]
        assert "diff hunks" in call["task_requirements"]

    def test_both_sources_run_two_calls_and_merge(self, monkeypatch) -> None:
        self._patch_collaborators(monkeypatch)
        whole_output = _FakeOutput(["whole-issue"], "whole summary", "")
        hunk_output = _FakeOutput(["hunk-issue"], "", "hunk notes")
        provider = _RecordingProvider([whole_output, hunk_output])
        kwargs = _run_reviewer_kwargs(head_files={"a.py": "content"}, code="### b.py ###\n1: y = 2")

        result = pr_review._run_reviewer(provider, **kwargs)

        assert len(provider.calls) == 2
        # Whole-file attempt dispatched before the hunk attempt.
        assert provider.calls[0]["pre_numbered"] is False
        assert provider.calls[1]["pre_numbered"] is True
        # Each attempt gets its own focus note and its own content, so a
        # regression that swaps the whole-file/hunk wiring (or drops the
        # hunk-mode pre_existing note) can't hide behind the flags above.
        assert provider.calls[0]["task_requirements"] == pr_review._whole_file_focus("PR body")
        assert provider.calls[0]["files"] == {"a.py": "content"}
        assert provider.calls[1]["task_requirements"] == pr_review._hunk_review_focus("PR body")
        assert "pre_existing" in provider.calls[1]["task_requirements"]
        assert "diff hunks" in provider.calls[1]["task_requirements"]
        assert provider.calls[1]["code"] == "### b.py ###\n1: y = 2"
        assert isinstance(result, pr_review._MergedReviewerOutput)
        assert result.issues == ["whole-issue", "hunk-issue"]

    def test_none_body_coerces_to_hunk_focus_note_alone(self, monkeypatch) -> None:
        self._patch_collaborators(monkeypatch)
        provider = _RecordingProvider([_FakeOutput([], "", "")])
        kwargs = _run_reviewer_kwargs(
            pr=_FakePR(body=None), head_files=None, code="### a.py ###\n1: x = 1"
        )

        pr_review._run_reviewer(provider, **kwargs)

        # A blank body still gets the hunk-mode focus note appended (the note
        # alone, since there is no body to prefix).
        assert provider.calls[0]["task_requirements"] == pr_review._hunk_review_focus("")
        # Content assertions alongside the equality check above, so a
        # simultaneous regression in _hunk_review_focus (e.g. back to
        # returning the body verbatim) can't hide behind both sides matching.
        assert "pre_existing" in provider.calls[0]["task_requirements"]
        assert "diff hunks" in provider.calls[0]["task_requirements"]

    def test_first_attempt_error_records_outage_and_stops(self, monkeypatch) -> None:
        outages = self._patch_collaborators(monkeypatch)
        provider = _RecordingProvider(error=RuntimeError("boom"))
        kwargs = _run_reviewer_kwargs(head_files={"a.py": "content"}, code="### b.py ###\n1: y = 2")

        result = pr_review._run_reviewer(provider, **kwargs)

        assert result is None
        assert len(provider.calls) == 1  # the hunk attempt never runs
        assert outages == [
            {
                "owner": "acme",
                "repo": "widgets",
                "pr_number": 7,
                "job_id": "job-1",
                "message": "code review failed: boom",
            }
        ]

    def test_bare_exception_falls_back_to_type_name(self, monkeypatch) -> None:
        outages = self._patch_collaborators(monkeypatch)
        provider = _RecordingProvider(error=RuntimeError())
        kwargs = _run_reviewer_kwargs(head_files={"a.py": "content"}, code="")

        result = pr_review._run_reviewer(provider, **kwargs)

        assert result is None
        assert outages[0]["message"] == "code review failed: RuntimeError (no error message)"

    def test_none_output_records_outage(self, monkeypatch) -> None:
        outages = self._patch_collaborators(monkeypatch)
        provider = _RecordingProvider([None])
        kwargs = _run_reviewer_kwargs(head_files={"a.py": "content"}, code="")

        result = pr_review._run_reviewer(provider, **kwargs)

        assert result is None
        assert outages[0]["message"] == "code review failed: reviewer returned no output"

    def test_no_sources_raises_assertion_error(self, monkeypatch) -> None:
        self._patch_collaborators(monkeypatch)
        provider = _RecordingProvider([])
        kwargs = _run_reviewer_kwargs(head_files=None, code="")

        with pytest.raises(AssertionError):
            pr_review._run_reviewer(provider, **kwargs)

    def test_nonempty_surface_alone_skips_whole_file(self, monkeypatch) -> None:
        self._patch_collaborators(monkeypatch)
        output = _FakeOutput(["issue"], "summary", "notes")
        provider = _RecordingProvider([output])
        surface = ChangeSurface(blocks={"mod.py": "1: def f():\n2:     return 1"})
        kwargs = _run_reviewer_kwargs(
            head_files=None,  # admission cleared surface-covered paths
            code="",
            change_surface=surface,
        )

        result = pr_review._run_reviewer(provider, **kwargs)

        assert result is output
        assert len(provider.calls) == 1
        call = provider.calls[0]
        assert call["pre_numbered"] is True
        assert call["code"] == surface.code
        assert "files" not in call
        assert call["task_requirements"] == pr_review._hunk_review_focus("PR body")

    def test_surface_plus_filtered_head_files_two_attempts(self, monkeypatch) -> None:
        self._patch_collaborators(monkeypatch)
        surface_out = _FakeOutput(["s"], "s", "")
        whole_out = _FakeOutput(["w"], "", "w")
        provider = _RecordingProvider([surface_out, whole_out])
        surface = ChangeSurface(blocks={"a.py": "1: a"})
        fallback_head = {"b.py": "whole b\n"}  # path-disjoint from surface
        kwargs = _run_reviewer_kwargs(
            head_files=fallback_head,
            code="",
            change_surface=surface,
        )

        result = pr_review._run_reviewer(provider, **kwargs)

        assert len(provider.calls) == 2
        assert provider.calls[0]["pre_numbered"] is True
        assert provider.calls[0]["code"] == surface.code
        assert "files" not in provider.calls[0]
        assert provider.calls[1]["pre_numbered"] is False
        assert provider.calls[1]["files"] == fallback_head
        assert set(provider.calls[1]["files"]).isdisjoint(surface.blocks)
        assert isinstance(result, pr_review._MergedReviewerOutput)
        assert result.issues == ["s", "w"]

    def test_empty_surface_keeps_whole_file(self, monkeypatch) -> None:
        self._patch_collaborators(monkeypatch)
        output = _FakeOutput(["issue"], "summary", "notes")
        provider = _RecordingProvider([output])
        kwargs = _run_reviewer_kwargs(
            head_files={"a.py": "content"},
            code="",
            change_surface=ChangeSurface(blocks={}),
        )

        result = pr_review._run_reviewer(provider, **kwargs)

        assert result is output
        assert len(provider.calls) == 1
        assert provider.calls[0]["pre_numbered"] is False
        assert provider.calls[0]["files"] == {"a.py": "content"}

    def test_surface_plus_hunk_code_two_prenumbred_no_files(self, monkeypatch) -> None:
        self._patch_collaborators(monkeypatch)
        surface_out = _FakeOutput(["s"], "s", "")
        hunk_out = _FakeOutput(["h"], "", "h")
        provider = _RecordingProvider([surface_out, hunk_out])
        surface = ChangeSurface(blocks={"a.py": "1: a"})
        kwargs = _run_reviewer_kwargs(
            head_files=None,
            code="### b.py ###\n1: y = 2",
            change_surface=surface,
        )

        result = pr_review._run_reviewer(provider, **kwargs)

        assert len(provider.calls) == 2
        assert provider.calls[0]["pre_numbered"] is True
        assert provider.calls[0]["code"] == surface.code
        assert "files" not in provider.calls[0]
        assert provider.calls[1]["pre_numbered"] is True
        assert provider.calls[1]["code"] == "### b.py ###\n1: y = 2"
        assert isinstance(result, pr_review._MergedReviewerOutput)
        assert result.issues == ["s", "h"]


# ---------------------------------------------------------------------------
# _finalize_review
# ---------------------------------------------------------------------------


class TestFinalizeReviewUnit:
    def test_rejects_non_terminal_status(self, monkeypatch) -> None:
        from software_engineering_team.models import JobStatus

        calls: List[Any] = []
        monkeypatch.setattr(
            main, "update_job", lambda *a, **kw: calls.append(("update_job", a, kw))
        )
        monkeypatch.setattr(
            main, "update_review", lambda *a, **kw: calls.append(("update_review", a, kw))
        )

        with pytest.raises(ValueError, match="COMPLETED or FAILED"):
            pr_review._finalize_review("job-1", JobStatus.RUNNING)
        assert calls == []

    def test_accepts_completed(self, monkeypatch) -> None:
        from software_engineering_team.models import JobStatus

        jobs: List[Dict[str, Any]] = []
        reviews: List[Dict[str, Any]] = []
        monkeypatch.setattr(main, "update_job", lambda job_id, **kw: jobs.append(kw))
        monkeypatch.setattr(main, "update_review", lambda job_id, **kw: reviews.append(kw))

        pr_review._finalize_review("job-1", JobStatus.COMPLETED, phase="completed")

        assert jobs[0]["status"] == JobStatus.COMPLETED.value
        assert reviews[0]["completed"] is True

    def test_accepts_failed(self, monkeypatch) -> None:
        from software_engineering_team.models import JobStatus

        jobs: List[Dict[str, Any]] = []
        reviews: List[Dict[str, Any]] = []
        monkeypatch.setattr(main, "update_job", lambda job_id, **kw: jobs.append(kw))
        monkeypatch.setattr(main, "update_review", lambda job_id, **kw: reviews.append(kw))

        pr_review._finalize_review("job-1", JobStatus.FAILED, phase="completed", error="boom")

        assert jobs[0]["status"] == JobStatus.FAILED.value
        assert jobs[0]["error"] == "boom"
        assert reviews[0]["completed"] is True
        assert reviews[0]["error"] == "boom"


# ---------------------------------------------------------------------------
# _build_change_surface_for_reviewable
# ---------------------------------------------------------------------------


class TestBuildChangeSurfaceForReviewable:
    def test_head_backed_usable_patch_builds_surface(self) -> None:
        content = "def f():\n    return 1\n"
        patch = "@@ -1,2 +1,2 @@\n def f():\n-    return 0\n+    return 1\n"
        files = [PullRequestFile("mod.py", "modified", patch, 1, 1, None)]
        head = {"mod.py": content}
        surface = pr_review._build_change_surface_for_reviewable(files, head)
        assert not surface.is_empty
        assert "mod.py" in surface.blocks
        assert "### mod.py ###" in surface.code

    def test_missing_head_omits_path_empty_surface(self) -> None:
        patch = "@@ -1,2 +1,2 @@\n def f():\n-    return 0\n+    return 1\n"
        files = [PullRequestFile("mod.py", "modified", patch, 1, 1, None)]
        surface = pr_review._build_change_surface_for_reviewable(files, {})
        assert surface.is_empty

    def test_removed_and_no_patch_excluded(self) -> None:
        content = "def f():\n    return 1\n"
        patch = "@@ -1,2 +1,2 @@\n def f():\n-    return 0\n+    return 1\n"
        files = [
            PullRequestFile("gone.py", "removed", patch, 0, 1, None),
            PullRequestFile("bin.png", "added", "", 0, 0, None),
            PullRequestFile("ok.py", "modified", patch, 1, 1, None),
        ]
        surface = pr_review._build_change_surface_for_reviewable(
            files, {"gone.py": content, "ok.py": content}
        )
        assert list(surface.blocks.keys()) == ["ok.py"]


# ---------------------------------------------------------------------------
# _decide_review_mode
# ---------------------------------------------------------------------------

# Patch/content pair that builds a non-empty change surface (mirrors
# TestBuildChangeSurfaceForReviewable.test_head_backed_usable_patch_builds_surface):
# one added line, matching head content.
_SURFACE_CONTENT = "def f():\n    return 1\n"
_SURFACE_PATCH = "@@ -1,2 +1,2 @@\n def f():\n-    return 0\n+    return 1\n"

# Deletion-only patch: removes a line, adds nothing. `extract_touched_lines`
# (added-only) is empty, so the change-surface builder always omits this path
# regardless of fetched head content -- it can only ever land in whole-file
# or hunk fallback.
_DELETION_ONLY_PATCH = "@@ -1,3 +1,2 @@\n def f():\n-    x = 1\n     return 1\n"
_DELETION_ONLY_CONTENT = "def f():\n    return 1\n"

# Removes every line with no replacement -- non-blank patch, but
# render_annotated_hunks has nothing to emit (no `+`/context lines).
_BLANK_RENDER_PATCH = "@@ -1,2 +0,0 @@\n-line1\n-line2\n"


class _FakeReviewPR:
    def __init__(
        self,
        head_sha: str = "deadbeef",
        html_url: str = "https://github.com/acme/widgets/pull/7",
    ) -> None:
        self.head_sha = head_sha
        self.html_url = html_url


class _FakeContentClient:
    """Fake GitHubClient for `_decide_review_mode`: `get_file_contents` keyed by filename.

    A filename absent from `contents` raises, mirroring a 404/fetch failure
    that `_fetch_head_files` degrades internally (never propagates).
    """

    def __init__(self, contents: Dict[str, str]) -> None:
        self._contents = contents

    def get_file_contents(self, owner: str, repo: str, filename: str, head_sha: str) -> str:
        if filename not in self._contents:
            raise RuntimeError(f"404: {filename}@{head_sha}")
        return self._contents[filename]


def _decide_review_mode_collaborators(monkeypatch):
    """Patch `_decide_review_mode`'s terminal-write collaborators.

    Returns `(comments, jobs, reviews)`, each appended to by the respective
    patched call so a noop path's courtesy comment and terminal write can be
    asserted on.
    """
    comments: List[Dict[str, Any]] = []
    jobs: List[Dict[str, Any]] = []
    reviews: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        main,
        "_safe_comment",
        lambda client, owner, repo, pr_number, comment: comments.append(
            {"owner": owner, "repo": repo, "pr_number": pr_number, "comment": comment}
        ),
    )
    monkeypatch.setattr(
        main, "update_job", lambda job_id, **kw: jobs.append({"job_id": job_id, **kw})
    )
    monkeypatch.setattr(
        main, "update_review", lambda job_id, **kw: reviews.append({"job_id": job_id, **kw})
    )
    return comments, jobs, reviews


class TestDecideReviewModeUnit:
    def test_no_files_completes_noop_and_returns_none(self, monkeypatch) -> None:
        comments, jobs, reviews = _decide_review_mode_collaborators(monkeypatch)
        client = _FakeContentClient({})
        pr = _FakeReviewPR()

        result = pr_review._decide_review_mode(client, "job-1", "acme", "widgets", 7, pr, [])

        assert result is None
        assert comments[0]["comment"] == "Code review: no changed files to review."
        assert jobs[0]["status"] == JobStatus.COMPLETED.value
        assert reviews[0]["status_text"] == "No changed files to review"
        assert jobs[0]["github_pr_url"] == pr.html_url

    def test_no_reviewable_files_completes_noop(self, monkeypatch) -> None:
        comments, _, _ = _decide_review_mode_collaborators(monkeypatch)
        client = _FakeContentClient({})
        pr = _FakeReviewPR()
        files = [
            PullRequestFile("gone.py", "removed", _SURFACE_PATCH, 0, 3, None),
            PullRequestFile("empty.py", "modified", "", 0, 0, None),
        ]

        result = pr_review._decide_review_mode(client, "job-1", "acme", "widgets", 7, pr, files)

        assert result is None
        assert comments[0]["comment"] == "Code review: no reviewable file content."

    def test_surface_covered_file_is_excluded_from_head_files(self, monkeypatch) -> None:
        """The fix this PR makes: a path the change surface covers must not
        also be handed to whole-file review -- `head_files` stays disjoint
        from `change_surface.blocks`."""
        _decide_review_mode_collaborators(monkeypatch)
        client = _FakeContentClient({"mod.py": _SURFACE_CONTENT})
        pr = _FakeReviewPR()
        files = [PullRequestFile("mod.py", "modified", _SURFACE_PATCH, 1, 1, None)]

        decision = pr_review._decide_review_mode(client, "job-1", "acme", "widgets", 7, pr, files)

        assert decision is not None
        assert not decision.change_surface.is_empty
        assert "mod.py" in decision.change_surface.blocks
        assert decision.head_files == {}
        assert decision.code == ""
        assert decision.files_reviewed == 1
        assert isinstance(decision.repo_reader, pr_review.GitHubRepoReader)

    def test_fetched_file_without_surface_coverage_uses_whole_file_fallback(
        self, monkeypatch
    ) -> None:
        _decide_review_mode_collaborators(monkeypatch)
        client = _FakeContentClient({"mod.py": _DELETION_ONLY_CONTENT})
        pr = _FakeReviewPR()
        files = [PullRequestFile("mod.py", "modified", _DELETION_ONLY_PATCH, 0, 1, None)]

        decision = pr_review._decide_review_mode(client, "job-1", "acme", "widgets", 7, pr, files)

        assert decision is not None
        assert decision.change_surface.is_empty
        assert decision.head_files == {"mod.py": _DELETION_ONLY_CONTENT}
        assert decision.code == ""
        assert decision.files_reviewed == 1

    def test_total_fetch_failure_falls_back_to_hunks(self, monkeypatch) -> None:
        _decide_review_mode_collaborators(monkeypatch)
        client = _FakeContentClient({})  # every fetch fails
        pr = _FakeReviewPR()
        files = [PullRequestFile("mod.py", "modified", _SURFACE_PATCH, 1, 1, None)]

        decision = pr_review._decide_review_mode(client, "job-1", "acme", "widgets", 7, pr, files)

        assert decision is not None
        assert decision.change_surface.is_empty
        assert decision.head_files == {}
        assert "### mod.py ###" in decision.code
        assert decision.files_reviewed == 1

    def test_total_failure_with_blank_hunk_render_is_a_noop(self, monkeypatch) -> None:
        """A file whose patch removes lines only (nothing for
        `render_annotated_hunks` to emit) and whose head fetch also fails
        leaves surface, head_files, AND hunk code all empty -- must degrade
        to the noop path, not a `ReviewModeDecision` carrying blank code."""
        comments, jobs, _ = _decide_review_mode_collaborators(monkeypatch)
        client = _FakeContentClient({})  # every fetch fails
        pr = _FakeReviewPR()
        files = [PullRequestFile("del.py", "modified", _BLANK_RENDER_PATCH, 0, 2, None)]

        result = pr_review._decide_review_mode(client, "job-1", "acme", "widgets", 7, pr, files)

        assert result is None
        assert comments[0]["comment"] == "Code review: no reviewable file content."
        assert jobs[0]["status"] == JobStatus.COMPLETED.value

    def test_mixed_surface_whole_file_and_hunk_partition_is_disjoint(self, monkeypatch) -> None:
        """One file per mode at once: surface-covered, whole-file fallback
        (fetched but surface-uncovered), and hunk fallback (fetch failed).
        The three sets must never overlap and every file counts exactly once."""
        _decide_review_mode_collaborators(monkeypatch)
        client = _FakeContentClient(
            {"surface.py": _SURFACE_CONTENT, "whole.py": _DELETION_ONLY_CONTENT}
        )
        pr = _FakeReviewPR()
        files = [
            PullRequestFile("surface.py", "modified", _SURFACE_PATCH, 1, 1, None),
            PullRequestFile("whole.py", "modified", _DELETION_ONLY_PATCH, 0, 1, None),
            PullRequestFile("hunk.py", "modified", _SURFACE_PATCH, 1, 1, None),
        ]

        decision = pr_review._decide_review_mode(client, "job-1", "acme", "widgets", 7, pr, files)

        assert decision is not None
        assert set(decision.change_surface.blocks) == {"surface.py"}
        assert set(decision.head_files) == {"whole.py"}
        assert "### hunk.py ###" in decision.code
        assert "surface.py" not in decision.code
        assert "whole.py" not in decision.code
        assert set(decision.change_surface.blocks).isdisjoint(decision.head_files)
        assert decision.files_reviewed == 3
