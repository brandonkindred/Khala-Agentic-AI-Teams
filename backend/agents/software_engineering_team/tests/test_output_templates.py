"""Tests for output_templates parsers bound in backend_code_v2_team and frontend_code_v2_team."""

from __future__ import annotations

import pytest

from software_engineering_team.codegen_team.stacks.backend import profile as be_tmpl
from software_engineering_team.codegen_team.stacks.frontend import profile as fe_tmpl


@pytest.mark.parametrize("tmpl", [be_tmpl, fe_tmpl], ids=["backend", "frontend"])
class TestSharedTemplates:
    def test_parse_files_and_summary_template(self, tmpl):
        text = (
            "## FILE src/foo.py ##\n"
            "def foo():\n    return 1\n"
            "## FILE src/bar.py ##\n"
            "def bar():\n    return 2\n"
            "## SUMMARY ##\n"
            "added two files\n"
            "## END SUMMARY ##\n"
        )
        result = tmpl.parse_files_and_summary_template(text)
        assert "src/foo.py" in result["files"]
        assert "src/bar.py" in result["files"]
        assert "def foo()" in result["files"]["src/foo.py"]
        assert result["summary"] == "added two files"

    def test_parse_files_no_end_summary_marker(self, tmpl):
        text = (
            "## FILE a.py ##\n"
            "x = 1\n"
            "## SUMMARY ##\n"
            "done\n"
        )
        result = tmpl.parse_files_and_summary_template(text)
        assert "a.py" in result["files"]
        assert result["summary"] == "done"

    def test_parse_planning_template(self, tmpl):
        text = (
            "## MICROTASKS ##\n"
            "id: m1\n"
            "description: first\n"
            "depends_on: \n"
            "---\n"
            "id: m2\n"
            "description: second\n"
            "depends_on: m1\n"
            "---\n"
            "## END MICROTASKS ##\n"
            "## LANGUAGE ##\n"
            "python\n"
            "## END LANGUAGE ##\n"
            "## SUMMARY ##\n"
            "plan ok\n"
            "## END SUMMARY ##\n"
        )
        result = tmpl.parse_planning_template(text)
        assert len(result["microtasks"]) == 2
        assert result["microtasks"][0]["id"] == "m1"
        assert result["microtasks"][1]["depends_on"] == ["m1"]
        assert result["summary"] == "plan ok"

    def test_parse_planning_template_no_end_markers(self, tmpl):
        text = "## MICROTASKS ##\nid: m1\ndescription: only\n## SUMMARY ##\nbla"
        result = tmpl.parse_planning_template(text)
        assert len(result["microtasks"]) == 1

    def test_parse_review_template_passed(self, tmpl):
        text = (
            "## PASSED ##\ntrue\n## END PASSED ##\n"
            "## ISSUES ##\n## END ISSUES ##\n"
            "## SUMMARY ##\nno issues\n## END SUMMARY ##\n"
        )
        result = tmpl.parse_review_template(text)
        assert result["passed"] is True
        assert result["issues"] == []
        assert result["summary"] == "no issues"

    def test_parse_review_template_failed_with_issues(self, tmpl):
        text = (
            "## PASSED ##\nfalse\n## END PASSED ##\n"
            "## ISSUES ##\n"
            "description: bad code\nseverity: high\nfile_path: x.py\nsource: code_review\n"
            "---\n"
            "description: meh\n"
            "---\n"
            "## END ISSUES ##\n"
            "## SUMMARY ##\nfix needed\n## END SUMMARY ##\n"
        )
        result = tmpl.parse_review_template(text)
        assert result["passed"] is False
        assert len(result["issues"]) == 2
        assert result["issues"][0]["severity"] == "high"
        assert result["issues"][1]["severity"] == "medium"  # default
        assert result["summary"] == "fix needed"

    def test_parse_review_template_no_end_markers(self, tmpl):
        """Falls back to scanning when end markers absent."""
        text = (
            "## PASSED ##\nyes\n"
            "## ISSUES ##\n"
            "description: x\n"
            "## SUMMARY ##\n"
            "done"
        )
        result = tmpl.parse_review_template(text)
        assert result["passed"] is True
        assert len(result["issues"]) == 1

    def test_parse_problem_solving_template(self, tmpl):
        text = (
            "## FILE a.py ##\nfixed\n"
            "## FIXES_APPLIED ##\n"
            "issue: bug\nfix: patch\n"
            "## END FIXES_APPLIED ##\n"
            "## RESOLVED ##\nyes\n## END RESOLVED ##\n"
            "## SUMMARY ##\nfixed all\n## END SUMMARY ##\n"
        )
        result = tmpl.parse_problem_solving_template(text)
        assert "a.py" in result["files"]
        assert len(result["fixes_applied"]) == 1
        assert result["resolved"] is True
        assert result["summary"] == "fixed all"

    def test_parse_problem_solving_single_issue_template(self, tmpl):
        text = (
            "## FILE a.py ##\nfixed\n"
            "## ROOT_CAUSE ##\nbad import\n## END ROOT_CAUSE ##\n"
            "## RESOLVED ##\nfalse\n## END RESOLVED ##\n"
            "## SUMMARY ##\npartial\n## END SUMMARY ##\n"
        )
        result = tmpl.parse_problem_solving_single_issue_template(text)
        assert result["root_cause"] == "bad import"
        assert result["resolved"] is False

    def test_parse_batch_fix_template(self, tmpl):
        text = (
            "## FILE a.py ##\ncode\n"
            "## ISSUES_ADDRESSED ##\n"
            "issue_index: 1\ndescription: fixed bug\n"
            "---\n"
            "issue_index: 2\ndescription: improved code\n"
            "## END ISSUES_ADDRESSED ##\n"
            "## SUMMARY ##\nall fixed\n## END SUMMARY ##\n"
        )
        result = tmpl.parse_batch_fix_template(text)
        assert "a.py" in result["files"]
        assert len(result["issues_addressed"]) == 2

    def test_parse_batch_fix_template_no_end_marker(self, tmpl):
        text = (
            "## FILE a.py ##\ncode\n"
            "## ISSUES_ADDRESSED ##\n"
            "issue_index: 1\ndescription: x\n"
            "## SUMMARY ##\nok\n"
        )
        result = tmpl.parse_batch_fix_template(text)
        assert len(result["issues_addressed"]) == 1

    def test_parse_documentation_self_review_template(self, tmpl):
        text = (
            "## QUALITY_SCORE ##\n0.85\n## END QUALITY_SCORE ##\n"
            "## IMPROVEMENTS ##\n"
            "- add docstring to foo\n"
            "  add typing hints\n"
            "- improve naming\n"
            "## END IMPROVEMENTS ##\n"
            "## FILE x.py ##\nupdated\n"
            "## SUMMARY ##\ndone\n## END SUMMARY ##\n"
        )
        result = tmpl.parse_documentation_self_review_template(text)
        assert result["quality_score"] == 0.85
        assert "x.py" in result["files"]
        assert "add docstring to foo" in result["improvements"]
        assert "improve naming" in result["improvements"]

    def test_parse_documentation_quality_clamps_range(self, tmpl):
        # Out of range -> clamped
        result = tmpl.parse_documentation_self_review_template(
            "## QUALITY_SCORE ##\n2.5\n## END QUALITY_SCORE ##\n"
        )
        assert result["quality_score"] == 1.0
        result = tmpl.parse_documentation_self_review_template(
            "## QUALITY_SCORE ##\n-1\n## END QUALITY_SCORE ##\n"
        )
        assert result["quality_score"] == 0.0

    def test_parse_documentation_invalid_score(self, tmpl):
        """Invalid quality score falls back to default 0.5."""
        result = tmpl.parse_documentation_self_review_template(
            "## QUALITY_SCORE ##\nnot a number\n## END QUALITY_SCORE ##\n"
        )
        assert result["quality_score"] == 0.5

    def test_section_returns_empty_when_marker_missing(self, tmpl):
        # internal _section
        assert tmpl._section("xx", "## A ##", "## END A ##") == ""

    def test_parse_files_skips_empty_path(self, tmpl):
        # Header with no path component is matched -- ensure empty paths are dropped
        text = "## FILE  ##\nbody\n## SUMMARY ##\ns\n## END SUMMARY ##\n"
        result = tmpl.parse_files_and_summary_template(text)
        # Empty path was filtered
        for k in result["files"]:
            assert k.strip()

    def test_parse_microtask_block_returns_none_for_no_id(self, tmpl):
        # Internal: a block with no id returns None
        # Reach via parse_planning_template
        text = "## MICROTASKS ##\ndescription: no id here\n## END MICROTASKS ##\n"
        result = tmpl.parse_planning_template(text)
        assert result["microtasks"] == []

    def test_parse_issue_block_returns_none_when_empty(self, tmpl):
        text = "## ISSUES ##\nfoo: bar\n## END ISSUES ##\n"  # no description/source
        result = tmpl.parse_review_template(text)
        assert result["issues"] == []


def test_frontend_normalize_file_path():
    """Frontend strips redundant frontend/ prefixes."""
    text = (
        "## FILE frontend/src/x.ts ##\ncontent\n"
        "## FILE ./frontend/src/y.ts ##\ncontent2\n"
        "## FILE src/z.ts ##\ncontent3\n"
        "## SUMMARY ##\nok\n## END SUMMARY ##\n"
    )
    result = fe_tmpl.parse_files_and_summary_template(text)
    assert "src/x.ts" in result["files"]
    assert "src/y.ts" in result["files"]
    assert "src/z.ts" in result["files"]
    assert "frontend/src/x.ts" not in result["files"]


def test_backend_normalize_file_path():
    """Backend strips redundant backend/ prefixes (if applicable)."""
    text = (
        "## FILE backend/api.py ##\nx\n"
        "## FILE ./backend/db.py ##\ny\n"
        "## FILE main.py ##\nz\n"
        "## SUMMARY ##\nok\n## END SUMMARY ##\n"
    )
    result = be_tmpl.parse_files_and_summary_template(text)
    # Backend may strip or keep prefix; we just confirm parsing succeeded
    assert len(result["files"]) == 3


def test_planning_template_language_variants():
    for lang in ("angular", "react", "vue", "typescript", "javascript"):
        text = (
            "## MICROTASKS ##\n"
            "id: m1\ndescription: x\n"
            "## END MICROTASKS ##\n"
            f"## LANGUAGE ##\n{lang}\n## END LANGUAGE ##\n"
        )
        result = fe_tmpl.parse_planning_template(text)
        assert result["language"] == lang


def test_planning_template_unknown_language_defaults():
    text = (
        "## MICROTASKS ##\nid: m1\ndescription: x\n## END MICROTASKS ##\n"
        "## LANGUAGE ##\nrust\n## END LANGUAGE ##\n"
    )
    result = fe_tmpl.parse_planning_template(text)
    # Default for frontend is typescript
    assert result["language"] == "typescript"


def test_review_template_pass_keywords():
    for kw in ("true", "yes", "1", "pass"):
        text = f"## PASSED ##\n{kw}\n## END PASSED ##\n"
        assert fe_tmpl.parse_review_template(text)["passed"] is True


def test_review_template_unknown_keyword_default_false():
    text = "## PASSED ##\nmaybe\n## END PASSED ##\n"
    assert fe_tmpl.parse_review_template(text)["passed"] is False
