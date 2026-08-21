"""Tests for software_engineering_team.shared.project_overview_builder.

Covers the build_project_overview helper with various input combinations:
empty inputs, prd_content only, full client_context, and partial client_context.
"""

from __future__ import annotations

import pytest

from software_engineering_team.shared.project_overview_builder import build_project_overview


class TestBuildProjectOverviewEmptyInputs:
    """When no arguments are provided, both fields should be empty strings."""

    def test_no_arguments(self) -> None:
        result = build_project_overview()
        assert result == {
            "features_and_functionality_doc": "",
            "goals": "",
        }

    def test_none_arguments(self) -> None:
        result = build_project_overview(prd_content=None, client_context=None)
        assert result == {
            "features_and_functionality_doc": "",
            "goals": "",
        }

    def test_empty_string_prd(self) -> None:
        """An empty string for prd_content is falsy; treated the same as None."""
        result = build_project_overview(prd_content="", client_context=None)
        assert result == {
            "features_and_functionality_doc": "",
            "goals": "",
        }

    def test_empty_dict_client_context(self) -> None:
        result = build_project_overview(prd_content=None, client_context={})
        assert result == {
            "features_and_functionality_doc": "",
            "goals": "",
        }


class TestBuildProjectOverviewPrdOnly:
    """When only prd_content is supplied."""

    def test_prd_content_only(self) -> None:
        prd = "# Features\n- Auth\n- Dashboard"
        result = build_project_overview(prd_content=prd)
        assert result["features_and_functionality_doc"] == prd
        assert result["goals"] == ""

    def test_prd_content_with_empty_context(self) -> None:
        prd = "Some PRD text"
        result = build_project_overview(prd_content=prd, client_context={})
        assert result["features_and_functionality_doc"] == prd
        assert result["goals"] == ""


class TestBuildProjectOverviewFullClientContext:
    """When both prd_content and a full client_context are provided."""

    def test_full_inputs(self) -> None:
        prd = "# Product Requirements"
        ctx = {
            "problem_summary": "Users cannot track expenses",
            "opportunity_statement": "Build an expense tracker app",
        }
        result = build_project_overview(prd_content=prd, client_context=ctx)

        expected_features = (
            "# Product Requirements\n\n"
            "## Problem summary\nUsers cannot track expenses\n\n"
            "## Opportunity\nBuild an expense tracker app"
        )
        assert result["features_and_functionality_doc"] == expected_features

        expected_goals = "Users cannot track expenses\nBuild an expense tracker app"
        assert result["goals"] == expected_goals

    def test_full_context_no_prd(self) -> None:
        ctx = {
            "problem_summary": "Pain point",
            "opportunity_statement": "Market gap",
        }
        result = build_project_overview(prd_content=None, client_context=ctx)

        expected_features = "## Problem summary\nPain point\n\n## Opportunity\nMarket gap"
        assert result["features_and_functionality_doc"] == expected_features
        assert result["goals"] == "Pain point\nMarket gap"


class TestBuildProjectOverviewPartialClientContext:
    """When client_context has only some of the recognised keys."""

    def test_only_problem_summary(self) -> None:
        ctx = {"problem_summary": "Slow onboarding"}
        result = build_project_overview(client_context=ctx)

        assert result["features_and_functionality_doc"] == "## Problem summary\nSlow onboarding"
        # goals should contain just the problem_summary (with empty opp stripped)
        assert result["goals"] == "Slow onboarding"

    def test_only_opportunity_statement(self) -> None:
        ctx = {"opportunity_statement": "Automate reporting"}
        result = build_project_overview(client_context=ctx)

        assert result["features_and_functionality_doc"] == "## Opportunity\nAutomate reporting"
        assert result["goals"] == "Automate reporting"

    def test_unrelated_keys_ignored(self) -> None:
        """Keys like target_users are not handled by this helper."""
        ctx = {
            "target_users": ["developer", "designer"],
            "some_other_key": 42,
        }
        result = build_project_overview(client_context=ctx)
        assert result["features_and_functionality_doc"] == ""
        assert result["goals"] == ""

    def test_problem_summary_empty_string(self) -> None:
        """Empty string values for context keys are treated as absent."""
        ctx = {
            "problem_summary": "",
            "opportunity_statement": "Real opportunity",
        }
        result = build_project_overview(client_context=ctx)
        assert result["features_and_functionality_doc"] == "## Opportunity\nReal opportunity"
        assert result["goals"] == "Real opportunity"


class TestBuildProjectOverviewReturnShape:
    """The return dict always has exactly the expected keys."""

    @pytest.mark.parametrize(
        "prd,ctx",
        [
            (None, None),
            ("prd", None),
            (None, {"problem_summary": "x"}),
            ("prd", {"problem_summary": "x", "opportunity_statement": "y"}),
        ],
    )
    def test_always_has_expected_keys(self, prd, ctx) -> None:
        result = build_project_overview(prd_content=prd, client_context=ctx)
        assert set(result.keys()) == {"features_and_functionality_doc", "goals"}
        assert isinstance(result["features_and_functionality_doc"], str)
        assert isinstance(result["goals"], str)
