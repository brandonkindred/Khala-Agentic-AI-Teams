"""
Blog compliance agent: Brand and Style Enforcer with veto power.

Evaluates drafts against the brand spec prompt and produces compliance_report.json.
FAIL status blocks publication and triggers the rewrite loop.

All errors are raised explicitly - no silent failures.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

from llm_service import LLMClient
from llm_service.interface import (
    LLMError as LLMServiceError,
)
from llm_service.interface import (
    LLMJsonParseError as LLMServiceJsonParseError,
)

from .models import ComplianceReport, Violation
from .prompts import COMPLIANCE_PROMPT

try:
    from shared.artifacts import write_artifact
    from shared.brand_spec import load_brand_spec_prompt
except ImportError:
    write_artifact = None
    load_brand_spec_prompt = None

try:
    from shared.errors import ComplianceError, LLMError
except ImportError:
    class ComplianceError(Exception):
        pass
    class LLMError(Exception):
        pass

_MAX_JSON_RETRIES = 2

logger = logging.getLogger(__name__)


class BlogComplianceAgent:
    """
    Expert agent that checks a draft against the brand spec and produces a compliance report.

    FAIL status triggers the orchestrator to block publication and enter the rewrite loop.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(
        self,
        draft: str,
        brand_spec_prompt: str,
        validator_report: Optional[Dict[str, Any]] = None,
        *,
        work_dir: Optional[Union[str, Path]] = None,
        on_llm_request: Optional[Callable[[str], None]] = None,
    ) -> ComplianceReport:
        """
        Evaluate the draft against the brand spec and produce a compliance report.

        Args:
            draft: The draft text to evaluate.
            brand_spec_prompt: Full brand spec prompt text (e.g. from brand_spec_prompt.md).
            validator_report: Optional validator_report.json content.
            work_dir: If provided, write compliance_report.json here.

        Returns:
            ComplianceReport with status PASS or FAIL.
        """
        brand_summary = (brand_spec_prompt or "").strip()

        # Pass only a concise summary of the validator report to avoid LLM echoing
        # long markdown content that breaks JSON parsing.
        if validator_report:
            checks = validator_report.get("checks", [])
            failed = [c.get("name", "unknown") for c in checks if c.get("status") == "FAIL"]
            validator_summary = (
                f"Overall: {validator_report.get('status', 'unknown')}. "
                f"Failed checks: {', '.join(failed) or 'none'}."
            )
        else:
            validator_summary = "No validator report available."

        prompt = COMPLIANCE_PROMPT.format(
            brand_spec_summary=brand_summary,
            validator_summary=validator_summary,
            draft=draft[:15000],
        )

        if on_llm_request:
            on_llm_request("Checking compliance with brand guidelines...")

        data = None
        for attempt in range(_MAX_JSON_RETRIES):
            current_prompt = prompt
            if attempt > 0:
                current_prompt = prompt + (
                    "\n\nCRITICAL: Your previous response contained invalid JSON. "
                    "Output ONLY a single valid JSON object. Do NOT embed code blocks, "
                    "markdown formatting, or literal newlines inside string values. "
                    "Keep evidence_quotes to under 80 characters each."
                )
            try:
                data = self.llm.complete_json(current_prompt, temperature=0.1)
                break
            except LLMServiceJsonParseError as e:
                logger.warning(
                    "Compliance JSON parse failed (attempt %d/%d): %s",
                    attempt + 1, _MAX_JSON_RETRIES, e,
                )
                continue
            except (LLMServiceError, LLMError):
                raise
            except Exception as e:
                logger.error("Compliance check failed: %s", e)
                raise ComplianceError(f"Compliance check failed: {e}", cause=e) from e

        if data is None:
            logger.warning(
                "Compliance JSON parse failed after %d attempts; using fallback FAIL report",
                _MAX_JSON_RETRIES,
            )
            report = ComplianceReport(
                status="FAIL",
                violations=[],
                required_fixes=["Could not parse compliance check result; re-run compliance."],
                notes=f"Fallback report: JSON parse failed after {_MAX_JSON_RETRIES} attempts.",
            )
            if work_dir and write_artifact:
                write_artifact(work_dir, "compliance_report.json", report.to_dict())
                logger.info("Wrote compliance_report.json: status=FAIL (fallback)")
            return report

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

    brand_path = brand_spec_path or (work_path / "brand_spec_prompt.md")
    if not Path(brand_path).exists():
        _blogging_root = Path(__file__).resolve().parent.parent
        brand_path = _blogging_root / "docs" / "brand_spec_prompt.md"
    if not load_brand_spec_prompt:
        raise ImportError("shared.brand_spec required")
    brand_spec_prompt = load_brand_spec_prompt(brand_path)

    agent = BlogComplianceAgent(llm_client=llm_client)
    return agent.run(draft, brand_spec_prompt, validator_report, work_dir=work_dir)
