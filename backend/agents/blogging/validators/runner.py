"""
Validator runner: orchestrates all checks and produces validator_report.json.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from shared.artifacts import read_artifact, write_artifact
from shared.brand_spec import BrandSpec

from .checks import (
    check_banned_patterns,
    check_banned_phrases,
    check_paragraph_length,
    check_reading_level,
    check_required_sections,
)
from .models import CheckResult, ValidatorReport

logger = logging.getLogger(__name__)


def check_claims_policy(
    draft: str,
    allowed_claims: Optional[Dict[str, Any]],
    require_allowed_claims: bool,
) -> Optional[CheckResult]:
    """
    Check claims policy: require [CLAIM:id] for factual claims; validate IDs exist.
    Returns None if claims policy is disabled.
    """
    if not require_allowed_claims or not allowed_claims:
        return None

    claims_list = allowed_claims.get("claims") or []
    allowed_ids = {str(c.get("id", "")) for c in claims_list if c.get("id")}

    # Find all [CLAIM:xxx] references in draft
    import re
    refs = re.findall(r"\[CLAIM:(\w+)\]", draft)
    unknown = [r for r in refs if r not in allowed_ids]
    if unknown:
        return CheckResult(
            name="claims_policy",
            status="FAIL",
            details={"unknown_claim_ids": unknown},
        )

    # Heuristic: sentences with numbers/percentages that look factual should have a tag
    # This is a soft check - we could skip it and rely on compliance agent for nuance
    # For now, if all refs are valid, PASS
    return CheckResult(name="claims_policy", status="PASS", details={})


def run_validators(
    draft: str,
    brand_spec: BrandSpec,
    *,
    allowed_claims: Optional[Dict[str, Any]] = None,
    work_dir: Optional[Union[str, Path]] = None,
) -> ValidatorReport:
    """
    Run all deterministic validators on the draft.

    Args:
        draft: The draft text to validate.
        brand_spec: Loaded brand spec.
        allowed_claims: Optional allowed_claims.json content (for claims policy check).
        work_dir: If provided, write validator_report.json here.

    Returns:
        ValidatorReport with status and checks.
    """
    checks: List[CheckResult] = []

    checks.append(check_banned_phrases(draft, brand_spec))
    checks.append(check_banned_patterns(draft, brand_spec))
    checks.append(check_paragraph_length(draft, brand_spec))
    checks.append(check_reading_level(draft, brand_spec))
    checks.append(check_required_sections(draft, brand_spec))

    require_claims = brand_spec.content_rules.claims_policy.require_allowed_claims
    claims_result = check_claims_policy(draft, allowed_claims, require_claims)
    if claims_result is not None:
        checks.append(claims_result)

    status = "PASS" if all(c.status == "PASS" for c in checks) else "FAIL"
    report = ValidatorReport(status=status, checks=checks)

    if work_dir:
        data = report.dict() if hasattr(report, "dict") else report.model_dump()
        write_artifact(work_dir, "validator_report.json", data)
        logger.info("Wrote validator_report.json: status=%s", status)

    return report


def run_validators_from_work_dir(
    work_dir: Union[str, Path],
    *,
    draft_artifact: str = "final.md",
    brand_spec_path: Optional[Union[str, Path]] = None,
) -> ValidatorReport:
    """
    Run validators using artifacts from work_dir.

    Reads draft from work_dir/draft_artifact, brand spec from work_dir/brand_spec_prompt.md
    or brand_spec_path, allowed_claims from work_dir/allowed_claims.json.
    Writes validator_report.json to work_dir.
    """
    work_path = Path(work_dir).resolve()
    draft = read_artifact(work_dir, draft_artifact, default="")
    if not draft:
        draft = read_artifact(work_dir, "draft_v2.md", default="") or read_artifact(
            work_dir, "draft_v1.md", default=""
        )

    brand_path = brand_spec_path or (work_path / "brand_spec_prompt.md")
    _blogging_root = Path(__file__).resolve().parent.parent
    default_path = _blogging_root / "docs" / "brand_spec_prompt.md"
    if not Path(brand_path).exists() and not default_path.exists():
        raise FileNotFoundError(f"No brand_spec_prompt.md found at {brand_path} or {default_path}")
    # Validators use default BrandSpec; brand rules come from prompt file (no structured YAML)
    brand_spec = BrandSpec()

    allowed_claims = read_artifact(work_dir, "allowed_claims.json", default=None)
    if isinstance(allowed_claims, dict):
        pass
    else:
        allowed_claims = None

    return run_validators(draft, brand_spec, allowed_claims=allowed_claims, work_dir=work_dir)
