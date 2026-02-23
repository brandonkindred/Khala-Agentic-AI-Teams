"""Tests for project planning template parsing (section-delimited, no JSON)."""

from __future__ import annotations

import pytest

from planning_team.project_planning_agent.output_templates import parse_project_planning_template


def test_parse_project_planning_template_full() -> None:
    """Parse full template output into overview fields."""
    text = """
## FEATURES_AND_FUNCTIONALITY ##
# Features
- Task CRUD API
- Multi-tenant isolation
## END FEATURES_AND_FUNCTIONALITY ##

## PRIMARY_GOAL ##
Deliver a multi-tenant task API.
## END PRIMARY_GOAL ##

## SECONDARY_GOALS ##
- Token auth
- OpenAPI spec
## END SECONDARY_GOALS ##

## DELIVERY_STRATEGY ##
Backend-first, then frontend.
## END DELIVERY_STRATEGY ##

## SCOPE_CUT ##
MVP: core API. V1: UI. Later: analytics.
## END SCOPE_CUT ##

## MILESTONES ##
---
id: M1
name: Backend
description: API and data
target_order: 0
scope_summary: API
definition_of_done: Tests pass
---
## END MILESTONES ##

## RISK_ITEMS ##
---
description: Scale
severity: medium
mitigation: Design for scale
---
## END RISK_ITEMS ##

## EPIC_STORY_BREAKDOWN ##
---
id: E1
name: API
description: REST API
scope: MVP
dependencies:
---
## END EPIC_STORY_BREAKDOWN ##

## NON_FUNCTIONAL_REQUIREMENTS ##
- Latency < 200ms
- Auth required
## END NON_FUNCTIONAL_REQUIREMENTS ##

## SUMMARY ##
Multi-tenant task API with OpenAPI.
## END SUMMARY ##
"""
    out = parse_project_planning_template(text)
    assert "Task CRUD" in out["features_and_functionality"]
    assert out["primary_goal"] == "Deliver a multi-tenant task API."
    assert "Token auth" in out["secondary_goals"]
    assert "Backend-first" in out["delivery_strategy"]
    assert "MVP" in out["scope_cut"]
    assert len(out["milestones"]) == 1
    assert out["milestones"][0]["id"] == "M1"
    assert out["milestones"][0]["name"] == "Backend"
    assert len(out["risk_items"]) == 1
    assert out["risk_items"][0]["description"] == "Scale"
    assert len(out["epic_story_breakdown"]) == 1
    assert out["epic_story_breakdown"][0]["id"] == "E1"
    assert "Latency" in out["non_functional_requirements"][0]
    assert "Multi-tenant" in out["summary"]


def test_parse_project_planning_template_tolerates_truncation() -> None:
    """Missing END markers still yield content (truncated response)."""
    text = """
## FEATURES_AND_FUNCTIONALITY ##
# Core
- API
- Auth
## PRIMARY_GOAL ##
Ship the API
"""
    out = parse_project_planning_template(text)
    assert "Core" in out["features_and_functionality"]
    assert "API" in out["features_and_functionality"]
    assert out["primary_goal"] == "Ship the API"
    assert out["milestones"] == []
    assert out["summary"] == ""


def test_parse_project_planning_template_empty_sections() -> None:
    """Empty or missing sections return defaults."""
    out = parse_project_planning_template("")
    assert out["features_and_functionality"] == ""
    assert out["primary_goal"] == ""
    assert out["secondary_goals"] == []
    assert out["milestones"] == []
    assert out["risk_items"] == []
    assert out["epic_story_breakdown"] == []
    assert out["summary"] == ""
