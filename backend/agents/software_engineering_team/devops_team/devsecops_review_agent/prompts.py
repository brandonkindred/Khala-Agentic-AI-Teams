"""Prompts for DevSecOps review agent."""

from software_engineering_team.shared.security_service import (
    SecurityProfile,
    build_review_prompt,
)

# Built from the unified Security Review service's ``infra`` profile, so the
# IAM/secrets/network focus and severity vocabulary live in one place (see
# ``shared/security_service.py``).
DEVSECOPS_REVIEW_PROMPT = build_review_prompt(SecurityProfile.INFRA)
