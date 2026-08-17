"""Tests for tech_lead_agent's review/merge prompt content."""

from software_engineering_team.shared.coding_standards import REVIEW_PRIORITY_FRAMEWORK
from software_engineering_team.tech_lead_agent import prompts


def test_code_review_system_includes_review_priority_framework():
    assert REVIEW_PRIORITY_FRAMEWORK in prompts.CODE_REVIEW_SYSTEM


def test_code_review_user_template_unchanged():
    assert "approved" in prompts.CODE_REVIEW_USER
    assert REVIEW_PRIORITY_FRAMEWORK not in prompts.CODE_REVIEW_USER
