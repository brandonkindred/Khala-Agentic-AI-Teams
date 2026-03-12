"""
Blog compliance agent: Brand and Style Enforcer with veto power.

Evaluates drafts against brand_spec and produces compliance_report.json.
FAIL status blocks publication and triggers the rewrite loop.

All errors are raised explicitly - no silent failures.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

from llm_service import LLMClient

from .models import ComplianceReport, Violation
from .prompts import COMPLIANCE_PROMPT

try:
    from shared.artifacts import write_artifact
    from shared.brand_spec import BrandSpec, load_brand_spec
except ImportError:
    write_artifact = None
    load_brand_spec = None
    BrandSpec = None

try:
    from shared.errors import ComplianceError, LLMError
except ImportError:
    class ComplianceError(Exception):
        pass
    class LLMError(Exception):
        pass

logger = logging.getLogger(__name__)


class BlogComplianceAgent:
    """
    Agent that checks a draft against the brand spec and produces a compliance report.

    FAIL status triggers the orchestrator to block publication and enter the rewrite loop.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(
        self,
        draft: str,
        brand_spec: BrandSpec,
        validator_report: Optional[Dict[str, Any]] = None,
        *,
        work_dir: Optional[Union[str, Path]] = None,
        on_llm_request: Optional[Callable[[str], None]] = None,
    ) -> ComplianceReport:
        """
        Evaluate the draft against the brand spec and produce a compliance report.

        Args:
            draft: The draft text to evaluate.
            brand_spec: Loaded brand spec.
            validator_report: Optional validator_report.json content.
            work_dir: If provided, write compliance_report.json here.

        Returns:
            ComplianceReport with status PASS or FAIL.
        """
        brand_summary = brand_spec.to_prompt_summary() if brand_spec else ""
        validator_str = json.dumps(validator_report, indent=2) if validator_report else "{}"

        prompt = COMPLIANCE_PROMPT.format(
            brand_spec_summary=brand_summary,
            validator_report=validator_str,
            draft=draft[:15000],
        )

        if on_llm_request:
            on_llm_request("Checking compliance with brand guidelines...")
        try:
            data = self.llm.complete_json(prompt, temperature=0.1)
        except LLMError:
            raise
        except Exception as e:
            logger.error("Compliance check failed: %s", e)
            raise ComplianceError(
                f"Compliance check failed: {e}",
                cause=e,
            ) from e

        status = (data.get("status") or "FAIL").upper()
        if status not in ("PASS", "FAIL"):
            status = "FAIL"

        raw_violations = data.get("violations") or []
        violations = []
        for v in raw_violations:
            if not isinstance(v, dict):
                continue
            violations.append(
                Violation(
                    rule_id=v.get("rule_id", "unknown"),
                    description=v.get("description", ""),
                    evidence_quotes=v.get("evidence_quotes") or [],
                    location_hint=v.get("location_hint"),
                )
            )

        required_fixes = data.get("required_fixes") or []
        if not isinstance(required_fixes, list):
            required_fixes = [str(required_fixes)] if required_fixes else []

        notes = data.get("notes")

        report = ComplianceReport(
            status=status,
            violations=violations,
            required_fixes=required_fixes,
            notes=notes,
        )

        if work_dir and write_artifact:
            write_artifact(work_dir, "compliance_report.json", report.to_dict())
            logger.info("Wrote compliance_report.json: status=%s", status)

        return report


def run_compliance_from_work_dir(
    work_dir: Union[str, Path],
    llm_client: LLMClient,
    *,
    draft_artifact: str = "final.md",
    brand_spec_path: Optional[Union[str, Path]] = None,
) -> ComplianceReport:
    """
    Run compliance agent using artifacts from work_dir.
    """
    try:
        from shared.artifacts import read_artifact
    except ImportError:
        raise ImportError("shared.artifacts required")

    work_path = Path(work_dir).resolve()
    draft = read_artifact(work_dir, draft_artifact, default="")
    if not draft:
        draft = read_artifact(work_dir, "draft_v2.md", default="") or read_artifact(
            work_dir, "draft_v1.md", default=""
        )

    validator_report = read_artifact(work_dir, "validator_report.json", default=None)

    brand_path = brand_spec_path or (work_path / "brand_spec.yaml")
    if not Path(brand_path).exists():
        _blogging_root = Path(__file__).resolve().parent.parent
        brand_path = _blogging_root / "docs" / "brand_spec.yaml"
    if not load_brand_spec:
        raise ImportError("shared.brand_spec required")
    brand_spec = load_brand_spec(brand_path)

    agent = BlogComplianceAgent(llm_client=llm_client)
    return agent.run(draft, brand_spec, validator_report, work_dir=work_dir)
