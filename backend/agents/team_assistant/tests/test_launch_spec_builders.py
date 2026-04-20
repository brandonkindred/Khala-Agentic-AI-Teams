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


@pytest.mark.parametrize("team_key", ["personal_assistant", "startup_advisor"])
def test_no_launch_spec_for_teams_without_workflows(team_key: str) -> None:
    assert TEAM_ASSISTANT_CONFIGS[team_key].launch_spec is None


# ---------------------------------------------------------------------------
# Track B: the 6 newly-onboarded teams
# ---------------------------------------------------------------------------


def test_branding_declarative_builder() -> None:
    built = _builder("branding")(
        {
            "company_name": "Acme",
            "company_description": "Sells anvils.",
            "target_audience": "cartoon coyotes",
            "values": "reliability",
        }
    )
    assert built.json == {
        "company_name": "Acme",
        "company_description": "Sells anvils.",
        "target_audience": "cartoon coyotes",
        "values": "reliability",
    }


def test_investment_builder_coerces_numeric_strings() -> None:
    """Conversation stores values as strings; the builder must cast to float/int."""
    built = _builder("investment")(
        {
            "user_id": "u-1",
            "risk_tolerance": "moderate",
            "max_drawdown_tolerance_pct": "20",  # string from the conversation
            "time_horizon_years": "10",
            "annual_gross_income": "150000",
            "tax_state": "CA",
        }
    )
    assert built.json == {
        "user_id": "u-1",
        "risk_tolerance": "moderate",
        "max_drawdown_tolerance_pct": 20.0,
        "time_horizon_years": 10,
        "annual_gross_income": 150000.0,
        "tax_state": "CA",
    }


def test_investment_builder_preserves_native_numeric_values() -> None:
    built = _builder("investment")(
        {
            "user_id": "u-2",
            "risk_tolerance": "aggressive",
            "max_drawdown_tolerance_pct": 35.5,
            "time_horizon_years": 20,
            "annual_gross_income": 500000,
        }
    )
    assert built.json["max_drawdown_tolerance_pct"] == 35.5
    assert built.json["time_horizon_years"] == 20
    assert built.json["annual_gross_income"] == 500000.0


def test_nutrition_builder_declarative() -> None:
    built = _builder("nutrition_meal_planning")({"client_id": "c-1", "message": "build me a plan"})
    assert built.json == {"client_id": "c-1", "message": "build me a plan"}


def test_agentic_team_provisioning_declarative() -> None:
    built = _builder("agentic_team_provisioning")(
        {"name": "QA Pod", "description": "handles QA automation"}
    )
    assert built.json == {"name": "QA Pod", "description": "handles QA automation"}


def test_user_agent_founder_emits_empty_body() -> None:
    """POST /start takes no parameters; the builder should send an empty JSON body."""
    built = _builder("user_agent_founder")({})
    assert built.json == {}
    assert built.files is None
    assert built.path_override is None


# ---------------------------------------------------------------------------
# Track C: sales redesign
# ---------------------------------------------------------------------------


def test_social_marketing_builder_injects_llm_model_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the user didn't pick a model, fall back to LLM_MODEL env var."""
    monkeypatch.setenv("LLM_MODEL", "llama3.1")
    built = _builder("social_marketing")(
        {
            "client_id": "c-1",
            "brand_id": "b-1",
            "goals": ["engagement"],
        }
    )
    assert built.json == {
        "client_id": "c-1",
        "brand_id": "b-1",
        "llm_model_name": "llama3.1",
        "goals": ["engagement"],
    }


def test_social_marketing_builder_prefers_context_value_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_MODEL", "llama3.1")
    built = _builder("social_marketing")(
        {
            "client_id": "c-1",
            "brand_id": "b-1",
            "llm_model_name": "mistral",
        }
    )
    assert built.json["llm_model_name"] == "mistral"


def test_social_marketing_builder_uses_hardcoded_default_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No context value, no env var → bundled docker/.env.example default."""
    monkeypatch.delenv("LLM_MODEL", raising=False)
    built = _builder("social_marketing")({"client_id": "c-1", "brand_id": "b-1"})
    assert built.json["llm_model_name"] == "qwen3.5:397b-cloud"


def test_sales_body_builder_decomposes_icp() -> None:
    built = _builder("sales_team")(
        {
            "product_name": "Khala",
            "value_proposition": "One API to run 20 agentic teams",
            "icp_industry": "SaaS, FinTech, DevTools",
            "icp_job_titles": "VP Engineering, CTO, Head of Platform",
            "icp_pain_points": "slow AI integration\nvendor lock-in\ncompliance overhead",
            "icp_company_size_min": "50",
            "icp_company_size_max": "2000",
            "icp_geographic_focus": "US, EU",
            "icp_tech_stack": "AWS, Kubernetes",
            "icp_disqualifying_traits": "solo consultant\nno cloud",
            "company_context": "YC W24 company",
            "case_study_snippets": "Acme ships 3x faster\nBeta Inc cut cost 40%",
            "entry_stage": "PROSPECTING",
            "max_prospects": "25",
        }
    )
    assert built.json == {
        "product_name": "Khala",
        "value_proposition": "One API to run 20 agentic teams",
        "entry_stage": "prospecting",
        "max_prospects": 25,
        "icp": {
            "industry": ["SaaS", "FinTech", "DevTools"],
            "job_titles": ["VP Engineering", "CTO", "Head of Platform"],
            "pain_points": ["slow AI integration", "vendor lock-in", "compliance overhead"],
            "company_size_min": 50,
            "company_size_max": 2000,
            "budget_range_usd": "$10k-$100k/yr",
            "geographic_focus": ["US", "EU"],
            "tech_stack_keywords": ["AWS", "Kubernetes"],
            "disqualifying_traits": ["solo consultant", "no cloud"],
        },
        "company_context": "YC W24 company",
        "case_study_snippets": ["Acme ships 3x faster", "Beta Inc cut cost 40%"],
    }


def test_sales_body_builder_uses_defaults_for_optional_fields() -> None:
    built = _builder("sales_team")(
        {
            "product_name": "Khala",
            "value_proposition": "One API",
            "icp_industry": "SaaS",
            "icp_job_titles": "CTO",
            "icp_pain_points": "slow integration",
        }
    )
    icp = built.json["icp"]
    assert icp["company_size_min"] == 10
    assert icp["company_size_max"] == 5000
    assert icp["budget_range_usd"] == "$10k-$100k/yr"
    assert icp["geographic_focus"] == []
    assert built.json["entry_stage"] == "prospecting"
    assert built.json["max_prospects"] == 5


def test_sales_body_builder_clamps_max_prospects_into_range() -> None:
    built = _builder("sales_team")(
        {
            "product_name": "Khala",
            "value_proposition": "v",
            "icp_industry": "x",
            "icp_job_titles": "y",
            "icp_pain_points": "z",
            "max_prospects": "500",
        }
    )
    assert built.json["max_prospects"] == 100

    built2 = _builder("sales_team")(
        {
            "product_name": "Khala",
            "value_proposition": "v",
            "icp_industry": "x",
            "icp_job_titles": "y",
            "icp_pain_points": "z",
            "max_prospects": "0",
        }
    )
    assert built2.json["max_prospects"] == 1
