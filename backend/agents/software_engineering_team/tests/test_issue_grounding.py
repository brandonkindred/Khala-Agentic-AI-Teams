"""Tests for software_engineering_team.shared.issue_grounding."""

from __future__ import annotations

from dataclasses import dataclass

from software_engineering_team.shared.issue_grounding import (
    drop_ungrounded_issues,
    extract_checkable_phrases,
    ground_issue_file_path,
)


@dataclass
class _Issue:
    source: str = "code_review"
    severity: str = "medium"
    description: str = ""
    file_path: str = ""
    recommendation: str = ""


def test_extract_checkable_phrases_title_case_and_quoted():
    # Avoid a sentence-initial capital glued onto the proper-noun run.
    text = 'index.html lacks Insurance Provider support; also see "Acme Health".'
    phrases = extract_checkable_phrases(text)
    assert "Insurance Provider" in phrases
    assert "Acme Health" in phrases
    # Single capitalized tokens are not checkable phrases.
    assert "Provider" not in phrases


def test_ground_issue_file_path_blanks_unknown_keeps_known():
    files = {"app/index.html": "<html></html>", "src/main.py": "pass"}
    assert ground_issue_file_path("app/index.html", files) == "app/index.html"
    assert ground_issue_file_path("main.py", files) == "src/main.py"  # basename alias
    assert ground_issue_file_path("missing.py", files) == ""
    assert ground_issue_file_path("", files) == ""


def test_drop_ungrounded_keeps_grounded_and_phrase_free():
    files = {"index.html": "<html></html>"}
    grounded = _Issue(
        description="Meal Planner does not render weekly view",
        file_path="index.html",
    )
    phrase_free = _Issue(
        description="off-by-one in the loop bound",
        file_path="index.html",
    )
    kept = drop_ungrounded_issues(
        [grounded, phrase_free],
        files=files,
        requirements="Build a Meal Planner with weekly view",
        acceptance_criteria=["weekly view works"],
        spec_content="",
    )
    assert len(kept) == 2
    assert kept[0].description == grounded.description
    assert kept[1].description == phrase_free.description


def test_drop_ungrounded_drops_fabricated_content_claims():
    files = {"index.html": "<html></html>"}
    fake = _Issue(
        description="index.html does not support Insurance Provider ZephyrCare",
        file_path="index.html",
        recommendation="Add ZephyrCare to the provider list",
    )
    dropped: list = []
    kept = drop_ungrounded_issues(
        [fake],
        files=files,
        requirements="Meal planning UI for weekly menus",
        acceptance_criteria=["user can plan meals"],
        spec_content="No insurance features",
        on_dropped=dropped.append,
    )
    assert kept == []
    assert len(dropped) == 1


def test_drop_ungrounded_blanks_bad_file_path_without_dropping():
    files = {"real.py": "x = 1"}
    issue = _Issue(
        description="off-by-one in the loop bound",
        file_path="hallucinated.py",
    )
    kept = drop_ungrounded_issues(
        [issue],
        files=files,
        requirements="Fix loop",
        acceptance_criteria=[],
        spec_content="",
    )
    assert len(kept) == 1
    assert kept[0].file_path == ""


def test_drop_ungrounded_recommendation_only_fabrication():
    files = {"app.py": "pass"}
    issue = _Issue(
        description="missing edge-case handling",
        file_path="app.py",
        recommendation='Wire up "Phantom Insurer" before merge',
    )
    kept = drop_ungrounded_issues(
        [issue],
        files=files,
        requirements="Harden error paths",
        acceptance_criteria=["errors are handled"],
        spec_content="",
    )
    assert kept == []


def test_drop_ungrounded_tolerates_none_in_acceptance_criteria():
    """None entries in acceptance_criteria must not raise (fail-open via TypeError)."""
    files = {"app.py": "pass"}
    issue = _Issue(description="off-by-one in the loop bound", file_path="app.py")
    kept = drop_ungrounded_issues(
        [issue],
        files=files,
        requirements="Fix loop",
        acceptance_criteria=["ok", None, "also ok"],  # type: ignore[list-item]
        spec_content="",
    )
    assert len(kept) == 1


def test_drop_ungrounded_fails_open_when_issue_raises():
    """Unexpected errors during grounding keep the issue (fail-open)."""

    class _Boom:
        file_path = "app.py"
        recommendation = ""

        @property
        def description(self) -> str:
            raise RuntimeError("malformed issue")

    kept = drop_ungrounded_issues(
        [_Boom()],
        files={"app.py": "pass"},
        requirements="anything",
        acceptance_criteria=[],
        spec_content="",
    )
    assert len(kept) == 1


def test_with_file_path_does_not_mutate_uncopyable_issue():
    """Non-dataclass / non-pydantic issues are left unchanged (no in-place mutate)."""

    class _Plain:
        def __init__(self) -> None:
            self.file_path = "missing.py"
            self.description = "off-by-one in the loop bound"
            self.recommendation = ""

    plain = _Plain()
    kept = drop_ungrounded_issues(
        [plain],
        files={"real.py": "x"},
        requirements="Fix loop",
        acceptance_criteria=[],
        spec_content="",
    )
    assert len(kept) == 1
    # Copy-only policy: path stays as cited when the type cannot be copied.
    assert kept[0].file_path == "missing.py"
    assert plain.file_path == "missing.py"


def test_drop_ungrounded_empty_files_dict():
    """Empty submission: unknown paths blanked (when copyable); phrase-free keeps."""
    issue = _Issue(description="off-by-one in the loop bound", file_path="any.py")
    kept = drop_ungrounded_issues(
        [issue],
        files={},
        requirements="Fix loop",
        acceptance_criteria=None,
        spec_content="",
    )
    assert len(kept) == 1
    assert kept[0].file_path == ""


def test_drop_ungrounded_all_none_acceptance_criteria():
    kept = drop_ungrounded_issues(
        [_Issue(description="off-by-one in the loop bound", file_path="a.py")],
        files={"a.py": "x"},
        requirements="Fix",
        acceptance_criteria=[None, None],  # type: ignore[list-item]
        spec_content="",
    )
    assert len(kept) == 1


def test_drop_ungrounded_missing_issue_attributes():
    """Objects lacking description/recommendation/file_path still ground safely."""

    class _Sparse:
        pass

    kept = drop_ungrounded_issues(
        [_Sparse()],
        files={"a.py": "x"},
        requirements="Anything",
        acceptance_criteria=[],
        spec_content="",
    )
    assert len(kept) == 1


def test_on_dropped_receives_issue_after_path_blanking():
    files = {"real.py": "x"}
    fake = _Issue(
        description='Missing "Phantom Insurer" support',
        file_path="missing.py",
    )
    dropped: list = []
    kept = drop_ungrounded_issues(
        [fake],
        files=files,
        requirements="No insurers here",
        acceptance_criteria=[],
        spec_content="",
        on_dropped=dropped.append,
    )
    assert kept == []
    assert len(dropped) == 1
    assert dropped[0].file_path == ""  # blanked before on_dropped
