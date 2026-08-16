"""Prompts for deployment strategy agent."""

from software_engineering_team.shared.prompts import build_json_output_prompt

DEPLOYMENT_STRATEGY_PROMPT = build_json_output_prompt(
    role_sentence="You are DeploymentStrategyAgent.",
    rules=(
        """Define deployment mechanics and release safety:
- rollout strategy (rolling, canary, blue/green)
- health checks and rollout timeout
- rollback path and trigger conditions
- environment-specific sequencing
- whether alerting is configured for the release

"""
    ),
    json_schema=(
        "- artifacts: object(path -> file_content)\n"
        "- strategy: string\n"
        "- rollback_plan: list[string]\n"
        "- health_checks: list[string]\n"
        "- rollout_timeout_minutes: number\n"
        "- alerting_configured: boolean\n"
        "- summary: string"
    ),
)
