"""Unit tests for prompt formatting and template-factory helpers (`prompts.py`)."""

from __future__ import annotations


def test_format_onboarding_summary_prompt_contains_inputs() -> None:
    from agent_team_studio.agent_provisioning_team.prompts import format_onboarding_summary_prompt

    p = format_onboarding_summary_prompt(agent_id="a1", tool_names="pg, redis")
    assert "a1" in p
    assert "pg" in p
    # The preamble always includes the anatomy reference text.
    assert "AGENT_ANATOMY" in p or "anatomy" in p.lower()


def test_format_tool_getting_started_prompt() -> None:
    from agent_team_studio.agent_provisioning_team.prompts import format_tool_getting_started_prompt

    p = format_tool_getting_started_prompt(
        tool_name="pg", description="db", connection_details="conn", permissions="r,w"
    )
    assert "pg" in p


def test_format_environment_overview_prompt() -> None:
    from agent_team_studio.agent_provisioning_team.prompts import format_environment_overview_prompt

    p = format_environment_overview_prompt(
        container_name="c1",
        workspace_path="/w",
        tools_list="- pg",
        env_vars="A=B",
    )
    assert "/w" in p


def test_format_ai_agent_create_prompt() -> None:
    from agent_team_studio.agent_provisioning_team.prompts import format_ai_agent_create_prompt

    p = format_ai_agent_create_prompt(requirements="build x")
    assert "build x" in p


def test_format_ai_agent_refine_prompt() -> None:
    from agent_team_studio.agent_provisioning_team.prompts import format_ai_agent_refine_prompt

    p = format_ai_agent_refine_prompt("current", "goals")
    assert "current" in p and "goals" in p


def test_onboarding_summary_template_factory() -> None:
    from agent_team_studio.agent_provisioning_team.prompts import onboarding_summary_prompt

    p = onboarding_summary_prompt()
    assert "{agent_id}" in p


def test_tool_getting_started_template_factory() -> None:
    from agent_team_studio.agent_provisioning_team.prompts import tool_getting_started_prompt

    p = tool_getting_started_prompt()
    assert "{tool_name}" in p


def test_environment_overview_template_factory() -> None:
    from agent_team_studio.agent_provisioning_team.prompts import environment_overview_prompt

    p = environment_overview_prompt()
    assert "{container_name}" in p


def test_ai_agent_create_template_factory() -> None:
    from agent_team_studio.agent_provisioning_team.prompts import ai_agent_create_prompt

    p = ai_agent_create_prompt()
    assert "{requirements}" in p


def test_ai_agent_refine_template_factory() -> None:
    from agent_team_studio.agent_provisioning_team.prompts import ai_agent_refine_prompt

    p = ai_agent_refine_prompt()
    assert "{current_definition}" in p
