"""
Blog compliance agent: Brand and Style Enforcer with veto power.

Evaluates drafts against the brand spec prompt and produces compliance_report.json.
FAIL status blocks publication and triggers the rewrite loop.

All errors are raised explicitly - no silent failures.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

from llm_service import (
    LLMClient,
    LLMJsonParseError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMTemporaryError,
    LLMTruncatedError,
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
    from shared.errors import ComplianceError
except ImportError:

    class ComplianceError(Exception):
        pass


logger = logging.getLogger(__name__)

# After llm_service exhausts HTTP retries, re-run complete_json a few times; then use a safe fallback report.
_MAX_COMPLIANCE_LLM_ROUNDS = 3
_JSON_RETRY_SUFFIX = (
    "\n\nRespond with a single JSON object only (no markdown, no code fences). "
    'Keys: "status", "violations", "required_fixes", "notes".'
)


def _fallback_compliance_report(exc: Exception) -> ComplianceReport:
    """When the LLM cannot return parseable JSON, fail closed with actionable guidance (no crash)."""
    return ComplianceReport(
        status="FAIL",
        violations=[],
        required_fixes=[
            "Automated brand compliance did not complete (LLM error). Re-run when the model is available, "
            "or review the draft against your brand spec manually."
        ],
        notes=(
            "Compliance check could not run to completion. This reflects a tooling/LLM failure, "
            f"not a verified brand finding. Error: {exc}"
        ),
    )


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

        data: Optional[Dict[str, Any]] = None
        base_prompt = prompt
        working_prompt = prompt
        for llm_round in range(_MAX_COMPLIANCE_LLM_ROUNDS):
            for json_attempt in range(2):
                try:
                    data = self.llm.complete_json(working_prompt, temperature=0.1)
                    break
                except LLMJsonParseError as e:
                    if json_attempt == 0:
                        logger.warning(
                            "Compliance JSON parse failed (attempt 1), retrying with strict instruction: %s",
                            e,
                        )
                        working_prompt = base_prompt + _JSON_RETRY_SUFFIX
                    else:
                        logger.warning(
                            "Compliance JSON parse failed after retry; using fallback report: %s",
                            e,
                        )
                        report = _fallback_compliance_report(e)
                        if work_dir and write_artifact:
                            write_artifact(work_dir, "compliance_report.json", report.to_dict())
                            logger.info(
                                "Wrote compliance_report.json (fallback): status=%s", report.status
                            )
                        return report
                except (LLMTemporaryError, LLMRateLimitError) as e:
                    if llm_round >= _MAX_COMPLIANCE_LLM_ROUNDS - 1:
                        logger.warning(
                            "Compliance LLM still failing after agent retries; using fallback report: %s",
                            e,
                        )
                        report = _fallback_compliance_report(e)
                        if work_dir and write_artifact:
                            write_artifact(work_dir, "compliance_report.json", report.to_dict())
                            logger.info(
                                "Wrote compliance_report.json (fallback): status=%s", report.status
                            )
                        return report
                    wait = min(60.0, 15.0 * (llm_round + 1))
                    logger.warning(
                        "Compliance LLM transport/transient error (round %d/%d, json attempt %d): %s — "
                        "sleeping %.0fs and retrying.",
                        llm_round + 1,
                        _MAX_COMPLIANCE_LLM_ROUNDS,
                        json_attempt + 1,
                        e,
                        wait,
                    )
                    time.sleep(wait)
                    working_prompt = base_prompt
                    break
                except (LLMPermanentError, LLMTruncatedError) as e:
                    logger.warning(
                        "Compliance LLM permanent/truncation error; using fallback report: %s", e
                    )
                    report = _fallback_compliance_report(e)
                    if work_dir and write_artifact:
                        write_artifact(work_dir, "compliance_report.json", report.to_dict())
                        logger.info(
                            "Wrote compliance_report.json (fallback): status=%s", report.status
                        )
                    return report
            if data is not None:
                break

        if not data:
            report = _fallback_compliance_report(
                RuntimeError("No compliance JSON after retries"),
            )
            if work_dir and write_artifact:
                write_artifact(work_dir, "compliance_report.json", report.to_dict())
                logger.info("Wrote compliance_report.json (fallback): status=%s", report.status)
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
