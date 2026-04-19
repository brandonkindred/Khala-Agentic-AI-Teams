"""Unit tests for the per-team launch body builders.

These exercise ``body_builder(context) -> BuiltBody`` in isolation — no
HTTP, no Postgres. Each builder lives in ``team_assistant.config`` or is
produced there via ``declarative_builder``.
"""

from __future__ import annotations

import pytest

from team_assistant.config import TEAM_ASSISTANT_CONFIGS
from team_assistant.launch_spec import BuiltBody


def _builder(team_key: str):
    spec = TEAM_ASSISTANT_CONFIGS[team_key].launch_spec
    assert spec is not None, f"{team_key} has no launch_spec"
    return spec.body_builder


def test_blogging_declarative_builder_copies_required_and_optional() -> None:
    built = _builder("blogging")(
        {
            "brief": "AI trends in 2026",
            "audience": "engineers",
            "tone_or_purpose": "educational",
            "content_profile": "technical_deep_dive",
        }
    )
    assert isinstance(built, BuiltBody)
    assert built.files is None
    assert built.json == {
        "brief": "AI trends in 2026",
        "audience": "engineers",
        "tone_or_purpose": "educational",
        "content_profile": "technical_deep_dive",
    }


def test_declarative_builder_skips_empty_optional_values() -> None:
    built = _builder("blogging")({"brief": "Minimal", "audience": ""})
    assert built.json == {"brief": "Minimal"}


def test_soc2_builder_requires_repo_path() -> None:
    built = _builder("soc2_compliance")({"repo_path": "/repo/x"})
    assert built.json == {"repo_path": "/repo/x"}


def test_market_research_declarative() -> None:
    spec = TEAM_ASSISTANT_CONFIGS["market_research"].launch_spec
    assert spec is not None and spec.synchronous is True
    built = spec.body_builder({"product_concept": "P", "target_users": "U", "business_goal": "G"})
    assert built.json == {"product_concept": "P", "target_users": "U", "business_goal": "G"}


def test_se_builder_prefers_repo_path_when_present() -> None:
    built = _builder("software_engineering")(
        {"repo_path": "/workspace/existing", "spec": "ignore me"}
    )
    assert built.files is None
    assert built.path_override is None
    assert built.json == {"repo_path": "/workspace/existing"}


def test_se_builder_uploads_spec_as_multipart_when_no_repo_path() -> None:
    built = _builder("software_engineering")(
        {
            "spec": "Build a todo app\nWith CRUD",
            "tech_stack": "React + FastAPI",
            "constraints": "ship in 1 week",
        }
    )
    assert built.json is None
    assert built.path_override == "/api/software-engineering/run-team/upload"
    assert built.form == {"project_name": "Build a todo app"}
    assert built.files is not None
    filename, content, content_type = built.files["spec_file"]
    assert filename == "initial_spec.md"
    assert content_type == "text/markdown"
    text = content.decode("utf-8")
    assert text.startswith("Build a todo app")
    assert "## Tech Stack\nReact + FastAPI" in text
    assert "## Constraints\nship in 1 week" in text


def test_se_builder_derives_default_project_name_for_empty_spec_first_line() -> None:
    # First line is filtered of all non-alphanum/space; fall back to default.
    built = _builder("software_engineering")({"spec": "###\nreal content"})
    assert built.form == {"project_name": "assistant-project"}


def test_accessibility_webpage_maps_audit_name_to_name_and_keeps_urls() -> None:
    built = _builder("accessibility_audit")(
        {
            "audit_name": "Marketing site WCAG",
            "audit_type": "webpage",
            "web_urls": ["https://example.com"],
            "wcag_levels": ["AA"],
        }
    )
    assert built.json == {
        "name": "Marketing site WCAG",
        "web_urls": ["https://example.com"],
        "wcag_levels": ["AA"],
    }


def test_accessibility_mobile_branch_uses_mobile_apps() -> None:
    built = _builder("accessibility_audit")(
        {
            "audit_name": "iOS checkout",
            "audit_type": "mobile",
            "web_urls": [{"platform": "ios", "bundle_id": "com.example"}],
        }
    )
    # Falls back to web_urls content when no explicit mobile_apps key.
    assert built.json["name"] == "iOS checkout"
    assert "mobile_apps" in built.json
    assert "web_urls" not in built.json


def test_road_trip_builder_nests_under_trip_key() -> None:
    built = _builder("road_trip_planning")(
        {
            "start_location": "SF",
            "travelers": [{"name": "You", "age_group": "adult"}],
            "trip_duration_days": 5,
            "preferences": ["scenic"],
        }
    )
    assert built.json == {
        "trip": {
            "start_location": "SF",
            "travelers": [{"name": "You", "age_group": "adult"}],
            "trip_duration_days": 5,
            "preferences": ["scenic"],
        }
    }


def test_deepthought_builder_passes_message_and_optional_depth() -> None:
    spec = TEAM_ASSISTANT_CONFIGS["deepthought"].launch_spec
    assert spec is not None and spec.synchronous is True
    built = spec.body_builder({"message": "What is love?", "max_depth": 3})
    assert built.json == {"message": "What is love?", "max_depth": 3}


@pytest.mark.parametrize("team_key", ["personal_assistant", "sales_team"])
def test_no_launch_spec_for_teams_without_workflows(team_key: str) -> None:
    assert TEAM_ASSISTANT_CONFIGS[team_key].launch_spec is None
