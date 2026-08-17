"""Prompts for runbook agent."""

from software_engineering_team.shared.prompts import build_json_output_prompt

DOC_RUNBOOK_PROMPT = build_json_output_prompt(
    role_sentence="You are DocumentationRunbookAgent.",
    rules=(
        """Create operational handoff artifacts:
- deployment steps
- rollback steps
- required approvals and change windows
- validation evidence summary

"""
    ),
    json_schema=("- files: object(path -> content)\n- summary: string"),
)
