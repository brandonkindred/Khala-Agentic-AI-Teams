"""Prompts for DevSecOps review agent."""

from software_engineering_team.shared.security_service import (
    SecurityProfile,
    build_review_prompt,
)

# Built from the unified Security Review service's ``infra`` profile, so the
# IAM/secrets/network focus and severity vocabulary live in one place (see
# ``shared/security_service.py``).
#
# Not migrated to shared/prompts/templates.py's build_json_output_prompt: this
# constant already routes through a shared builder (build_review_prompt), and
# security_service.py backs other non-devops consumers too, so folding it into
# build_json_output_prompt is out of this migration's scope.
DEVSECOPS_REVIEW_PROMPT = build_review_prompt(SecurityProfile.INFRA)
