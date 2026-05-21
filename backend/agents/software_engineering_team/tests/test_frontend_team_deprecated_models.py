"""Tests for ``frontend_team_deprecated.models`` summarization helpers."""

from __future__ import annotations


def test_summarize_ux_none_returns_empty() -> None:
    from software_engineering_team.frontend_team_deprecated.models import _summarize_ux

    assert _summarize_ux(None) == ""


def test_summarize_ux_with_fields() -> None:
    from software_engineering_team.frontend_team_deprecated.models import (
        UXDesignerOutput,
        _summarize_ux,
    )

    ux = UXDesignerOutput(
        user_journeys="Login flow",
        interaction_rules="Click rules",
        microcopy_guidelines="Be friendly",
        summary="UX done",
    )
    text = _summarize_ux(ux)
    assert "User Journeys" in text
    assert "Login flow" in text
    assert "UX Summary" in text


def test_summarize_ux_empty_fields_returns_empty() -> None:
    from software_engineering_team.frontend_team_deprecated.models import (
        UXDesignerOutput,
        _summarize_ux,
    )

    assert _summarize_ux(UXDesignerOutput()) == ""


def test_summarize_ui_with_fields() -> None:
    from software_engineering_team.frontend_team_deprecated.models import (
        UIDesignerOutput,
        _summarize_ui,
    )

    ui = UIDesignerOutput(
        component_specs="Button: blue",
        design_tokens="primary=#00f",
        motion_guidelines="ease-in",
        summary="UI summary",
    )
    text = _summarize_ui(ui)
    assert "Component Specs" in text
    assert "primary=#00f" in text


def test_summarize_ui_none() -> None:
    from software_engineering_team.frontend_team_deprecated.models import _summarize_ui

    assert _summarize_ui(None) == ""


def test_summarize_design_system() -> None:
    from software_engineering_team.frontend_team_deprecated.models import (
        DesignSystemOutput,
        _summarize_design_system,
    )

    ds = DesignSystemOutput(
        component_library_plan="Library plan",
        token_implementation_plan="Tokens",
        a11y_in_components="AA",
        summary="DS",
    )
    text = _summarize_design_system(ds)
    assert "Component Library Plan" in text
    assert "Token Implementation" in text
    assert "A11y in Components" in text


def test_summarize_design_system_none() -> None:
    from software_engineering_team.frontend_team_deprecated.models import (
        _summarize_design_system,
    )

    assert _summarize_design_system(None) == ""


def test_summarize_architect() -> None:
    from software_engineering_team.frontend_team_deprecated.models import (
        FrontendArchitectOutput,
        _summarize_architect,
    )

    arch = FrontendArchitectOutput(
        folder_structure="src/",
        routing_strategy="lazy",
        state_management="ngrx",
        error_handling="global",
        api_client_patterns="HttpClient",
        summary="arch summary",
    )
    text = _summarize_architect(arch)
    assert "Folder Structure" in text
    assert "Routing Strategy" in text
    assert "State Management" in text
    assert "Error Handling" in text
    assert "API Client Patterns" in text


def test_summarize_architect_none() -> None:
    from software_engineering_team.frontend_team_deprecated.models import (
        _summarize_architect,
    )

    assert _summarize_architect(None) == ""


def test_build_feature_implementation_context_empty() -> None:
    from software_engineering_team.frontend_team_deprecated.models import (
        build_feature_implementation_context,
    )

    assert build_feature_implementation_context() == ""


def test_build_feature_implementation_context_all() -> None:
    from software_engineering_team.frontend_team_deprecated.models import (
        DesignSystemOutput,
        FrontendArchitectOutput,
        UIDesignerOutput,
        UXDesignerOutput,
        build_feature_implementation_context,
    )

    text = build_feature_implementation_context(
        ux=UXDesignerOutput(summary="UX"),
        ui=UIDesignerOutput(summary="UI"),
        design_system=DesignSystemOutput(summary="DS"),
        architect=FrontendArchitectOutput(summary="ARCH"),
    )
    assert "Design & UX Context" in text
    assert "UI & Visual Design Context" in text
    assert "Design System Context" in text
    assert "Architecture Context" in text
