"""Prompts for the Infrastructure Patch agent."""

from software_engineering_team.shared.prompts import build_json_output_prompt

INFRA_PATCH_PROMPT = build_json_output_prompt(
    role_sentence=(
        "You are an expert Infrastructure Patch Specialist. Given classified IaC errors and the "
        "current artifact files, produce minimal fixes."
    ),
    rules=(
        """Rules:
1. Only fix errors that are classified as "syntax" or "validation".
2. Return the COMPLETE updated file contents for each file that needs changes.
3. Make minimal changes -- do NOT refactor or add features.
4. Preserve all existing resources, outputs, and variables unless they are the source of the error.

"""
    ),
    json_schema=(
        """{
  "patched_artifacts": {
    "main.tf": "... full corrected file content ...",
    "variables.tf": "... full corrected file content ..."
  },
  "summary": "Brief description of fixes applied",
  "edits_applied": 2
}"""
    ),
    trailer="If no fixes can be produced, return empty patched_artifacts and explain in summary.\n",
)
