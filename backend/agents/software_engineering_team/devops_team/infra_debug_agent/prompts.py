"""Prompts for the Infrastructure Debug agent."""

from software_engineering_team.shared.prompts import build_json_output_prompt

INFRA_DEBUG_PROMPT = build_json_output_prompt(
    role_sentence=(
        "You are an Infrastructure Debug Specialist. Analyze the execution output from an IaC "
        "tool and classify each error."
    ),
    rules=(
        """For each error found, provide:
- error_type: one of "syntax", "state", "permissions", "resource_conflict", "validation", "runtime", "unknown"
- tool: the IaC tool name
- file_path: the file where the error originates (if identifiable)
- line_number: the line number (if identifiable)
- error_message: a concise description of the error

Also determine whether ALL errors are fixable via code changes (syntax, validation errors are fixable; permissions, state, runtime typically are not).

"""
    ),
    json_schema=(
        """{
  "errors": [
    {
      "error_type": "syntax",
      "tool": "terraform",
      "file_path": "main.tf",
      "line_number": 15,
      "error_message": "Missing closing brace"
    }
  ],
  "summary": "Brief summary of findings",
  "fixable": true
}"""
    ),
    trailer="",
)
